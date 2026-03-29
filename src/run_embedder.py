"""
Phase 3 — Description Generation + Embedding

For every table already indexed in the metadata DB:
  1. Build a structured text blob (table description + column descriptions + FK context)
  2. Enrich columns_info JSONB with per-column descriptions
  3. Embed the text blob with BAAI/bge-m3
  4. Write table_description, updated columns_info, and embedding back to table_metadata

Prerequisites:
  - Phase 2 must have run (table_metadata and table_relationships must be populated)
  - Run migrations/001_add_embedding_column.sql against nl2sql_metadata first

Run from src/:
    python run_phase3.py
    python run_phase3.py ../env/.env ERPHUB   # explicit env path + DB name
"""
import sys
import os
import json

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

from metadata_store import get_metadata_connection
from description_embedder.description_generator import (
    generate_table_description,
    build_embedding_text,
    enrich_columns_with_descriptions,
)
from description_embedder.embedder import Embedder


# ── DB helpers ────────────────────────────────────────────────────────────────

def load_tables(conn, db_name: str) -> list[dict]:
    """Return all table_metadata rows for the given registered database."""
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
    """
    Return {source_table: {source_column: (target_table, target_column)}}.
    Used to detect FK columns and generate "references X.y" descriptions.
    """
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


def load_related_tables(conn, db_name: str) -> dict[str, list[str]]:
    """
    Return {table: [directly related tables]} treating FK edges as undirected.
    These appear in the "Related tables:" line of the embedding text.
    """
    query = """
        SELECT tr.source_table, tr.target_table
        FROM table_relationships tr
        JOIN registered_databases rd ON rd.id = tr.db_id
        WHERE rd.db_name = %(db_name)s;
    """
    related: dict[str, set] = {}
    with conn.cursor() as cur:
        cur.execute(query, {"db_name": db_name})
        for row in cur.fetchall():
            src, tgt = row['source_table'], row['target_table']
            related.setdefault(src, set()).add(tgt)
            related.setdefault(tgt, set()).add(src)
    return {k: sorted(v) for k, v in related.items()}


def update_table_row(
    conn,
    table_id: int,
    description: str,
    enriched_columns: list[dict],
    embedding: list[float],
) -> None:
    """
    Write description, enriched columns_info, and embedding back to one row.
    The embedding is passed as a bracketed string and cast to vector by pgvector.
    """
    embedding_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

    query = """
        UPDATE table_metadata
        SET
            table_description = %(description)s,
            columns_info      = %(columns_info)s::jsonb,
            embedding         = %(embedding)s::vector,
            updated_at        = now()
        WHERE id = %(id)s;
    """
    with conn.cursor() as cur:
        cur.execute(query, {
            "id":           table_id,
            "description":  description,
            "columns_info": json.dumps(enriched_columns),
            "embedding":    embedding_str,
        })
    conn.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def run(env_path: str = DEFAULT_ENV_PATH, db_name: str = None) -> None:
    load_dotenv(env_path)

    if db_name is None:
        db_name = os.getenv("POSTGRES_DB_NAME")

    # ── Step 1: load metadata ────────────────────────────────────────────────
    print(f"[1/4] Connecting to metadata DB...")
    conn = get_metadata_connection(env_path=env_path)

    print(f"[2/4] Loading tables and FK relationships for '{db_name}'...")
    tables      = load_tables(conn, db_name)
    col_fk_maps = load_col_fk_maps(conn, db_name)
    related     = load_related_tables(conn, db_name)
    print(f"      {len(tables)} tables loaded.")

    # ── Step 2: build text blobs ─────────────────────────────────────────────
    print("[3/4] Building description text blobs...")
    texts            = []
    descriptions     = []
    enriched_cols    = []

    for row in tables:
        table_name   = row['table_name']
        columns      = row['columns_info']          # list[dict] — psycopg2 parses JSONB
        col_fk_map   = col_fk_maps.get(table_name, {})
        table_related = related.get(table_name, [])

        table_desc = generate_table_description(table_name)
        text       = build_embedding_text(
            table_name=table_name,
            table_description=table_desc,
            columns=columns,
            col_fk_map=col_fk_map,
            related_tables=table_related,
        )
        enriched   = enrich_columns_with_descriptions(columns, col_fk_map)

        texts.append(text)
        descriptions.append(table_desc)
        enriched_cols.append(enriched)

    print(f"      Built {len(texts)} text blobs.")

    # ── Step 3: embed ────────────────────────────────────────────────────────
    print("[4/4] Generating embeddings with BAAI/bge-m3...")
    embedder   = Embedder()
    embeddings = embedder.embed(texts)
    print(f"      {len(embeddings)} embeddings generated.")

    # ── Step 4: write back ───────────────────────────────────────────────────
    print("      Writing descriptions and embeddings to metadata DB...")
    for i, row in enumerate(tables):
        update_table_row(
            conn,
            table_id        = row['id'],
            description     = descriptions[i],
            enriched_columns= enriched_cols[i],
            embedding       = embeddings[i],
        )
        print(f"        [{i+1:>3}/{len(tables)}] {row['table_name']}")

    conn.close()

    print("\n── Phase 3 complete ─────────────────────────────────")
    print(f"  Database        : {db_name}")
    print(f"  Tables embedded : {len(tables)}")
    print(f"  Embedding dim   : 1024")
    print(f"  Model           : BAAI/bge-m3")
    print("─────────────────────────────────────────────────────")


if __name__ == "__main__":
    env = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ENV_PATH
    db  = sys.argv[2] if len(sys.argv) > 2 else None
    run(env_path=env, db_name=db)
