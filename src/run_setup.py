"""
Schema-extraction worker.

Connects to ONE target PostgreSQL database, extracts DDL, FK relationships,
and pre-computed two-hop paths, and writes them into the metadata DB.

In the current architecture this function is invoked per row by
`run_index_unindexed.py` for every registered_databases row that has no
metadata yet. The per-row credentials come from the metadata DB itself
(decrypted via pgcrypto), not from env vars.

The legacy `python run_setup.py` CLI is kept for ad-hoc local dev: it falls
back to POSTGRES_* env vars and indexes a single DB whose name comes from
POSTGRES_DB_NAME.
"""
import os
import sys
from dataclasses import dataclass

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

from logger import get_logger
from schema_extractor.list_tables import list_table_names
from schema_extractor.ddl_extractor import extract_table_ddl
from schema_extractor.fk_extractor import extract_foreign_keys
from schema_extractor.path_builder import build_adjacency, find_two_hop_paths
from metadata_store import (
    get_metadata_connection,
    register_database,
    get_db_id_by_name,
    store_table_metadata,
    store_fk_relationships,
    store_two_hop_paths,
)


@dataclass
class TargetDBCredentials:
    """Connection details for one target DB. Used by index_one_db()."""
    db_name: str
    host: str
    port: int
    schema_name: str
    db_user: str
    db_password: str


def _connect_target(creds: TargetDBCredentials) -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=creds.host,
        port=creds.port,
        database=creds.db_name,
        user=creds.db_user,
        password=creds.db_password,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )


def index_one_db(
    creds: TargetDBCredentials,
    meta_conn: psycopg2.extensions.connection,
    db_id: int,
) -> tuple[int, int, int]:
    """
    Extract schema from one target DB and write it to the metadata DB.

    `db_id` is the existing registered_databases.id for this row. The caller is
    responsible for clearing prior metadata (clear_db_indexed_data) before
    invoking this when retrying a failed indexing run.

    Returns (table_count, fk_count, two_hop_count) for logging.
    """
    logger = get_logger(__name__)

    logger.info("[index] '%s' @ %s:%s — connecting", creds.db_name, creds.host, creds.port)
    target_conn = _connect_target(creds)

    try:
        tables = list_table_names(target_conn)
        logger.info("[index] '%s' — %d tables", creds.db_name, len(tables))

        fks = extract_foreign_keys(target_conn)
        logger.info("[index] '%s' — %d FK relationships", creds.db_name, len(fks))

        adjacency = build_adjacency(fks)
        two_hop_paths = find_two_hop_paths(adjacency)
        logger.info("[index] '%s' — %d two-hop paths", creds.db_name, len(two_hop_paths))

        for i, table_name in enumerate(tables, start=1):
            table_ddl = extract_table_ddl(connection=target_conn, table_name=table_name)
            store_table_metadata(conn=meta_conn, db_id=db_id, table_ddl=table_ddl)
            logger.debug("[index] '%s' [%3d/%d] %s", creds.db_name, i, len(tables), table_name)

        store_fk_relationships(conn=meta_conn, db_id=db_id, fks=fks)
        store_two_hop_paths(conn=meta_conn, db_id=db_id, paths=two_hop_paths)
    finally:
        target_conn.close()

    return len(tables), len(fks), len(two_hop_paths)


# ── Legacy CLI: env-driven, single-DB. Kept for ad-hoc local dev. ──────────────

def _run_from_env(env_path: str = DEFAULT_ENV_PATH) -> None:
    logger = get_logger(__name__)
    load_dotenv(env_path)

    creds = TargetDBCredentials(
        db_name     = os.getenv("POSTGRES_DB_NAME"),
        host        = os.getenv("DATABASE_IP"),
        port        = int(os.getenv("DATABASE_PORT", "5432")),
        schema_name = "public",
        db_user     = os.getenv("POSTGRES_USER"),
        db_password = os.getenv("POSTGRES_PASSWORD"),
    )
    missing = [k for k, v in creds.__dict__.items() if not v]
    if missing:
        raise ValueError(f"Missing target DB env vars: {missing}")

    meta_conn = get_metadata_connection(env_path=env_path)
    try:
        db_id = get_db_id_by_name(meta_conn, creds.db_name)
        if db_id is None:
            db_id = register_database(
                conn=meta_conn,
                db_name=creds.db_name,
                host=creds.host,
                port=creds.port,
                schema_name=creds.schema_name,
            )
        tables, fks, paths = index_one_db(creds, meta_conn, db_id)
        logger.info(
            "Setup complete — DB: %s | tables: %d | FKs: %d | two-hop paths: %d",
            creds.db_name, tables, fks, paths,
        )
    finally:
        meta_conn.close()


if __name__ == "__main__":
    env = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ENV_PATH
    _run_from_env(env_path=env)
