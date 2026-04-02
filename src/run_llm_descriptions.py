"""
LLM Description Generation

For every table already indexed in the metadata DB, calls an LLM endpoint to
generate:
  - A one-sentence table description
  - A brief description for each column

Updates table_description and columns_info in the metadata DB.
After this script completes, run run_embedder.py to re-embed with the new descriptions.

Usage (run from src/):
    python run_llm_descriptions.py <llm_base_url> [model] [db_name]

Examples:
    python run_llm_descriptions.py http://1.2.3.4:8000/v1
    python run_llm_descriptions.py http://1.2.3.4:8000/v1 cyankiwi/Qwen3-30B-A3B-Instruct-2507-AWQ-4bit
    python run_llm_descriptions.py http://1.2.3.4:8000/v1 cyankiwi/Qwen3-30B-A3B-Instruct-2507-AWQ-4bit ERPHUB
"""
import sys
import os
import json

from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

from metadata_store import get_metadata_connection
from description_embedder.llm_describer import LLMDescriber


DEFAULT_MODEL = "cyankiwi/Qwen3-30B-A3B-Instruct-2507-AWQ-4bit"


# ── DB helpers ────────────────────────────────────────────────────────────────

def load_tables(conn, db_name: str) -> list[dict]:
    query = """
        SELECT tm.id, tm.table_name, tm.schema_name, tm.columns_info
        FROM table_metadata tm
        JOIN registered_databases rd ON rd.id = tm.db_id
        WHERE rd.db_name = %(db_name)s
        ORDER BY tm.table_name;
    """
    with conn.cursor() as cur:
        cur.execute(query, {"db_name": db_name})
        return cur.fetchall()


def load_col_fk_maps(conn, db_name: str) -> dict[str, dict[str, tuple]]:
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


def update_descriptions(
    conn,
    table_id: int,
    description: str,
    enriched_columns: list[dict],
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
            "description":  description,
            "columns_info": json.dumps(enriched_columns),
        })
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def run(
    llm_base_url: str,
    model: str = DEFAULT_MODEL,
    db_name: str = None,
    env_path: str = DEFAULT_ENV_PATH,
) -> None:
    load_dotenv(env_path)

    if db_name is None:
        db_name = os.getenv("POSTGRES_DB_NAME")

    print(f"[1/3] Connecting to metadata DB...")
    conn = get_metadata_connection(env_path=env_path)

    print(f"[2/3] Loading tables for '{db_name}'...")
    tables      = load_tables(conn, db_name)
    col_fk_maps = load_col_fk_maps(conn, db_name)
    print(f"      {len(tables)} tables loaded.")

    print(f"[3/3] Generating descriptions via LLM...")
    print(f"      Endpoint : {llm_base_url}")
    print(f"      Model    : {model}")

    describer = LLMDescriber(base_url=llm_base_url, model=model)

    success = 0
    skipped = 0

    for i, row in enumerate(tables):
        table_name = row['table_name']
        columns    = row['columns_info']
        col_fk_map = col_fk_maps.get(table_name, {})

        print(f"  [{i+1:>3}/{len(tables)}] {table_name}", end=" ", flush=True)

        result = describer.describe_table(table_name, columns, col_fk_map)
        if result is None:
            print("→ skipped")
            skipped += 1
            continue

        table_desc = result.get("table_description", f"Stores {table_name} records")
        col_descs  = result.get("columns", {})

        # merge LLM descriptions into columns_info
        enriched = []
        for col in columns:
            enriched_col = dict(col)
            enriched_col['description'] = col_descs.get(col['name'], col.get('description', ''))
            enriched.append(enriched_col)

        update_descriptions(conn, row['id'], table_desc, enriched)
        print("→ ok")
        success += 1

    conn.close()

    print("\n── LLM description generation complete ──────────────────")
    print(f"  Database  : {db_name}")
    print(f"  Success   : {success}")
    print(f"  Skipped   : {skipped}")
    print(f"  Next step : python run_embedder.py")
    print("─────────────────────────────────────────────────────────")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python run_llm_descriptions.py <llm_base_url> [model] [db_name]")
        print("Example: python run_llm_descriptions.py http://1.2.3.4:8000/v1")
        sys.exit(1)

    llm_url = sys.argv[1]
    model   = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_MODEL
    db      = sys.argv[3] if len(sys.argv) > 3 else None

    run(llm_base_url=llm_url, model=model, db_name=db)
