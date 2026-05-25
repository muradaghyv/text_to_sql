"""
Optional LLM-driven description enrichment.

For every (or one specific) registered DB, calls an LLM endpoint to generate
a one-sentence table description and a short per-column description, then
re-embeds the affected tables with BGE-M3 so vector search uses the new text.

Default behavior is **fill-empty** — the LLM result is only written for
tables and columns whose description is currently empty. A `--force` run
overwrites every description.

Default output language is Azerbaijani (the project's deployment audience).

Usage from inside the container:
    docker compose run --rm api describe                       # all DBs, az, fill-empty
    docker compose run --rm api describe --force               # overwrite everything
    docker compose run --rm api describe --db ERPHUB --lang en

Usage from a local checkout:
    python run_llm_descriptions.py [--db DB_NAME] [--lang az|en] [--force] \
                                   [--llm-base-url URL] [--model NAME]

`--llm-base-url` and `--model` default to the LLM_BASE_URL and LLM_MODEL env
vars when not passed.
"""
import argparse
import json
import os
import sys

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

from logger import get_logger
from metadata_store import get_metadata_connection
from description_embedder.llm_describer import LLMDescriber
from description_embedder.embedder import Embedder
from run_embedder import embed_one_db


# ── DB helpers ────────────────────────────────────────────────────────────────

def _list_indexed_db_names(conn) -> list[str]:
    """All registered DBs that have at least one table_metadata row and no error."""
    query = """
        SELECT rd.db_name
        FROM registered_databases rd
        JOIN (
            SELECT db_id, COUNT(*) AS cnt
            FROM table_metadata
            GROUP BY db_id
        ) tm ON tm.db_id = rd.id
        WHERE rd.indexing_error IS NULL AND tm.cnt > 0
        ORDER BY rd.id;
    """
    with conn.cursor() as cur:
        cur.execute(query)
        return [r['db_name'] for r in cur.fetchall()]


def _load_tables(conn, db_name: str) -> list[dict]:
    query = """
        SELECT tm.id, tm.table_name, tm.schema_name,
               tm.columns_info, tm.table_description
        FROM table_metadata tm
        JOIN registered_databases rd ON rd.id = tm.db_id
        WHERE rd.db_name = %(db_name)s
        ORDER BY tm.table_name;
    """
    with conn.cursor() as cur:
        cur.execute(query, {"db_name": db_name})
        return cur.fetchall()


def _load_col_fk_maps(conn, db_name: str) -> dict[str, dict[str, tuple]]:
    query = """
        SELECT tr.source_table, tr.source_column, tr.target_table, tr.target_column
        FROM table_relationships tr
        JOIN registered_databases rd ON rd.id = tr.db_id
        WHERE rd.db_name = %(db_name)s;
    """
    fk_maps: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute(query, {"db_name": db_name})
        for row in cur.fetchall():
            fk_maps.setdefault(row['source_table'], {})[row['source_column']] = (
                row['target_table'], row['target_column']
            )
    return fk_maps


def _write_descriptions(
    conn,
    table_id: int,
    table_description: str,
    columns: list[dict],
) -> None:
    query = """
        UPDATE table_metadata
        SET
            table_description = %(description)s,
            columns_info      = %(columns_info)s::jsonb,
            updated_at        = now()
        WHERE id = %(id)s;
    """
    with conn.cursor() as cur:
        cur.execute(query, {
            "id":           table_id,
            "description":  table_description,
            "columns_info": json.dumps(columns),
        })
    conn.commit()


# ── Per-table merge logic ─────────────────────────────────────────────────────

def _table_needs_describing(row: dict, force: bool) -> bool:
    if force:
        return True
    if not row['table_description']:
        return True
    for col in (row['columns_info'] or []):
        if not col.get('description'):
            return True
    return False


def _merge_table_description(existing: str, llm_value: str, force: bool) -> str:
    if force:
        return llm_value or existing
    return existing or llm_value


def _merge_columns(
    existing_columns: list[dict],
    llm_columns: dict[str, str],
    force: bool,
) -> list[dict]:
    merged = []
    for col in existing_columns:
        new_col = dict(col)
        llm_value = llm_columns.get(col['name'], '') or ''
        existing_value = col.get('description') or ''
        new_col['description'] = (
            (llm_value or existing_value) if force
            else (existing_value or llm_value)
        )
        merged.append(new_col)
    return merged


# ── Main ──────────────────────────────────────────────────────────────────────

def run(
    llm_base_url: str,
    model: str,
    db_name: str | None = None,
    lang: str = "az",
    force: bool = False,
    env_path: str = DEFAULT_ENV_PATH,
) -> None:
    logger = get_logger(__name__)
    load_dotenv(env_path)

    meta_conn = get_metadata_connection(env_path=env_path)
    try:
        if db_name:
            target_dbs = [db_name]
        else:
            target_dbs = _list_indexed_db_names(meta_conn)

        if not target_dbs:
            logger.warning(
                "No indexed target DBs found. Insert rows into "
                "registered_databases and re-run docker compose up first."
            )
            return

        logger.info(
            "describe — dbs=%s lang=%s force=%s",
            target_dbs, lang, force,
        )

        describer = LLMDescriber(base_url=llm_base_url, model=model, lang=lang)
        embedder: Embedder | None = None

        for current_db in target_dbs:
            tables      = _load_tables(meta_conn, current_db)
            col_fk_maps = _load_col_fk_maps(meta_conn, current_db)
            logger.info("[describe] '%s' — %d tables", current_db, len(tables))

            updated = 0
            skipped = 0
            failed  = 0

            for i, row in enumerate(tables, start=1):
                table_name = row['table_name']
                columns    = row['columns_info'] or []
                col_fk_map = col_fk_maps.get(table_name, {})

                if not _table_needs_describing(row, force):
                    skipped += 1
                    logger.debug("[describe] [%3d/%d] %s — already documented",
                                 i, len(tables), table_name)
                    continue

                try:
                    result = describer.describe_table(table_name, columns, col_fk_map)
                except Exception as e:
                    logger.error("[describe] [%3d/%d] %s — LLM error: %s",
                                 i, len(tables), table_name, e)
                    failed += 1
                    continue

                if result is None:
                    failed += 1
                    continue

                llm_table_desc = result.get("table_description", "") or ""
                llm_columns    = result.get("columns", {}) or {}

                merged_table_desc = _merge_table_description(
                    row['table_description'] or "", llm_table_desc, force,
                )
                merged_columns = _merge_columns(columns, llm_columns, force)

                _write_descriptions(meta_conn, row['id'], merged_table_desc, merged_columns)
                updated += 1
                logger.info("[describe] [%3d/%d] %s — updated", i, len(tables), table_name)

            logger.info(
                "[describe] '%s' — %d updated, %d skipped, %d failed",
                current_db, updated, skipped, failed,
            )

            if updated == 0:
                continue

            # Re-embed so vector search reflects the new descriptions.
            if embedder is None:
                logger.info("Loading BGE-M3 embedder for re-embedding...")
                embedder = Embedder()
            embed_one_db(meta_conn, current_db, embedder)

    finally:
        meta_conn.close()


# ── CLI ───────────────────────────────────────────────────────────────────────

def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Generate LLM descriptions for indexed target DBs."
    )
    p.add_argument(
        "--db",
        help="Limit to one db_name (default: every indexed registered DB).",
    )
    p.add_argument(
        "--lang",
        choices=["az", "en"],
        default="az",
        help="Language for generated descriptions (default: az).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing descriptions (default: fill empty only).",
    )
    p.add_argument(
        "--llm-base-url",
        default=os.getenv("LLM_BASE_URL"),
        help="OpenAI-compatible endpoint (default: $LLM_BASE_URL).",
    )
    p.add_argument(
        "--model",
        default=os.getenv("LLM_MODEL"),
        help="Model name (default: $LLM_MODEL).",
    )
    return p.parse_args(argv)


if __name__ == "__main__":
    args = _parse_args(sys.argv[1:])
    if not args.llm_base_url:
        sys.exit("error: --llm-base-url not set and LLM_BASE_URL env var is empty")
    if not args.model:
        sys.exit("error: --model not set and LLM_MODEL env var is empty")

    run(
        llm_base_url=args.llm_base_url,
        model=args.model,
        db_name=args.db,
        lang=args.lang,
        force=args.force,
    )
