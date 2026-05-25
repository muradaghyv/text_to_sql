"""
Orchestrator that runs at container startup before the API does.

Two phases:
  1. Schema indexing — for every row in registered_databases that has no
     table_metadata yet (or had indexing_error set), connect to the target DB,
     extract DDL/FKs/two-hop paths, embed with BGE-M3, write to metadata DB.
  2. Privilege sync — for every row with use_privilege_sync=true AND empty
     emp_table_access, connect to the target DB and run the ERPHUB-style
     fuzzy match → emp_table_access pipeline.

Behavior on partial failure:
  • Indexing exceptions are caught. Child rows for that db_id are deleted
    (clean slate). The error is written to registered_databases.indexing_error.
    The next row is attempted; the script never aborts startup.
  • Privilege-sync exceptions are caught and logged but do NOT block the API.
    Empty emp_table_access just means that DB serves 403 until fixed.

A single Embedder (BGE-M3) is loaded once and reused across DBs.

Required env vars:
    METADATA_DB_HOST, METADATA_DB_PORT, METADATA_DB_USER, METADATA_DB_NAME,
    METADATA_DB_PASSWORD, DB_CRED_ENCRYPTION_KEY
"""
import os
import sys

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

from logger import get_logger
from metadata_store import (
    get_metadata_connection,
    list_unindexed_dbs,
    list_dbs_needing_privilege_sync,
    clear_db_indexed_data,
    set_indexing_error,
    clear_indexing_error,
)
from run_setup import TargetDBCredentials, index_one_db
from run_embedder import embed_one_db
from run_privilege_sync import (
    TargetDBCredentials as PrivSyncCredentials,
    sync_one_db as sync_privileges_one_db,
)
from description_embedder.embedder import Embedder


def run(env_path: str = DEFAULT_ENV_PATH) -> None:
    logger = get_logger(__name__)
    load_dotenv(env_path)

    encryption_key = os.getenv("DB_CRED_ENCRYPTION_KEY")
    if not encryption_key:
        raise ValueError("DB_CRED_ENCRYPTION_KEY is required to decrypt target DB passwords")

    meta_conn = get_metadata_connection(env_path=env_path)
    try:
        rows = list_unindexed_dbs(meta_conn, encryption_key)

        if not rows:
            logger.info("All registered DBs are already indexed.")
        else:
            logger.info("Found %d target DB(s) needing indexing: %s",
                        len(rows), [r["db_name"] for r in rows])

        embedder: Embedder | None = None
        succeeded: list[str] = []
        failed: list[tuple[str, str]] = []

        for row in rows:
            db_name = row["db_name"]
            db_id   = row["id"]
            try:
                missing_creds = [
                    k for k in ("host", "port", "db_user", "db_password")
                    if not row.get(k)
                ]
                if missing_creds:
                    raise ValueError(
                        f"registered_databases row is missing required fields: {missing_creds}"
                    )

                creds = TargetDBCredentials(
                    db_name     = db_name,
                    host        = row["host"],
                    port        = int(row["port"]),
                    schema_name = row["schema_name"] or "public",
                    db_user     = row["db_user"],
                    db_password = row["db_password"],
                )

                # Wipe any partial state from a prior failed attempt.
                clear_db_indexed_data(meta_conn, db_id)

                index_one_db(creds, meta_conn, db_id)

                # Lazy-load BGE-M3 once on the first DB that gets this far.
                if embedder is None:
                    logger.info("Loading BGE-M3 embedder (one-time)...")
                    embedder = Embedder()

                embed_one_db(meta_conn, db_name, embedder)
                clear_indexing_error(meta_conn, db_id)

                succeeded.append(db_name)
                logger.info("✓ Indexed and embedded '%s'", db_name)

            except Exception as e:
                logger.error("✗ Failed to index '%s': %s", db_name, e, exc_info=True)
                # Best effort: clear partial state so the next attempt is clean.
                try:
                    clear_db_indexed_data(meta_conn, db_id)
                except Exception:
                    logger.exception("Cleanup also failed for '%s' (db_id=%s)", db_name, db_id)
                set_indexing_error(meta_conn, db_id, str(e))
                failed.append((db_name, str(e)))
                continue

        if rows:
            logger.info(
                "Indexing run complete — succeeded: %d, failed: %d",
                len(succeeded), len(failed),
            )
            if failed:
                for name, err in failed:
                    logger.warning("  failed: %s — %s", name, err)
                logger.warning(
                    "API will start anyway; failed DBs are skipped until "
                    "their registered_databases row is fixed and the container restarts."
                )

        # ── Phase 2: privilege sync for flagged DBs ──────────────────────────
        priv_rows = list_dbs_needing_privilege_sync(meta_conn, encryption_key)
        if not priv_rows:
            logger.info("No DBs need privilege sync.")
            return

        logger.info(
            "Privilege sync needed for %d DB(s): %s",
            len(priv_rows), [r["db_name"] for r in priv_rows],
        )

        for row in priv_rows:
            db_name = row["db_name"]
            db_id   = row["id"]
            try:
                missing = [k for k in ("host", "port", "db_user", "db_password")
                           if not row.get(k)]
                if missing:
                    raise ValueError(
                        f"registered_databases row is missing required fields: {missing}"
                    )

                creds = PrivSyncCredentials(
                    db_name     = db_name,
                    host        = row["host"],
                    port        = int(row["port"]),
                    schema_name = row["schema_name"] or "public",
                    db_user     = row["db_user"],
                    db_password = row["db_password"],
                )
                sync_privileges_one_db(creds, meta_conn, db_id)
                logger.info("✓ Privilege sync complete for '%s'", db_name)
            except Exception as e:
                # Don't block startup — empty emp_table_access just means 403s
                # until the admin fixes whatever's wrong.
                logger.error(
                    "✗ Privilege sync failed for '%s': %s",
                    db_name, e, exc_info=True,
                )
    finally:
        meta_conn.close()


if __name__ == "__main__":
    env = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ENV_PATH
    run(env_path=env)
