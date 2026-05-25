"""
Description-text + BGE-M3 embedding for one target DB.

For every table belonging to a registered DB, this module:
  1. Builds a structured text blob (table description + column descriptions
     + FK context + related-tables line)
  2. Enriches columns_info JSONB with per-column descriptions (kept where
     present, generated structurally otherwise)
  3. Embeds the text blob with BAAI/bge-m3
  4. Writes table_description (only when missing), updated columns_info, and
     the 1024-dim embedding back to table_metadata.

Public surface:
    embed_one_db(meta_conn, db_name, embedder) — embed every table for db_name
                                                  using the supplied Embedder.

The legacy `python run_embedder.py [env_path] [db_name]` CLI is kept for
ad-hoc local dev — it loads its own Embedder and connects via env vars.
"""
import os
import sys
import json

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

from logger import get_logger
from metadata_store import get_metadata_connection
from description_embedder.description_generator import (
    generate_table_description,
    build_embedding_text,
    enrich_columns_with_descriptions,
)
from description_embedder.embedder import Embedder


# ── DB helpers ────────────────────────────────────────────────────────────────

def _load_tables(conn, db_name: str) -> list[dict]:
    """Return all table_metadata rows for the given registered database."""
    query = """
        SELECT tm.id, tm.table_name, tm.schema_name, tm.columns_info, tm.table_description
        FROM table_metadata tm
        JOIN registered_databases rd ON rd.id = tm.db_id
        WHERE rd.db_name = %(db_name)s
        ORDER BY tm.table_name;
    """
    with conn.cursor() as cur:
        cur.execute(query, {"db_name": db_name})
        return cur.fetchall()


def _load_col_fk_maps(conn, db_name: str) -> dict[str, dict[str, tuple]]:
    """
    Return {source_table: {source_column: (target_table, target_column)}}.
    Used to detect FK columns and to label them "references X.y".
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


def _load_related_tables(conn, db_name: str) -> dict[str, list[str]]:
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


def _update_table_row(
    conn,
    table_id: int,
    description: str,
    enriched_columns: list[dict],
    embedding: list[float],
) -> None:
    """
    Write description, enriched columns_info, and embedding back to one row.
    The embedding is passed as a bracketed string and cast to vector by pgvector.

    `description` is only written when the existing value is empty — this
    prevents the embedder's structural fallback ("Stores X records") from
    overwriting a pg_description comment or a real LLM-generated description.
    """
    embedding_str = "[" + ",".join(f"{v:.8f}" for v in embedding) + "]"

    query = """
        UPDATE table_metadata
        SET
            table_description = COALESCE(NULLIF(table_description, ''), %(description)s),
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


# ── Public entry point ────────────────────────────────────────────────────────

def embed_one_db(
    meta_conn,
    db_name: str,
    embedder: Embedder,
) -> int:
    """
    Build text blobs and embeddings for every table of `db_name` and write
    them back. Returns the number of tables embedded.
    """
    logger = get_logger(__name__)

    tables      = _load_tables(meta_conn, db_name)
    col_fk_maps = _load_col_fk_maps(meta_conn, db_name)
    related     = _load_related_tables(meta_conn, db_name)

    if not tables:
        logger.warning("[embed] '%s' — no tables found in metadata; skipping", db_name)
        return 0

    logger.info("[embed] '%s' — building text blobs for %d tables", db_name, len(tables))

    texts          = []
    descriptions   = []
    enriched_cols  = []

    for row in tables:
        table_name    = row['table_name']
        columns       = row['columns_info']
        col_fk_map    = col_fk_maps.get(table_name, {})
        table_related = related.get(table_name, [])

        # Existing pg_description comment wins; fall back to structural sentence.
        table_desc = row['table_description'] or generate_table_description(table_name)
        text = build_embedding_text(
            table_name        = table_name,
            table_description = table_desc,
            columns           = columns,
            col_fk_map        = col_fk_map,
            related_tables    = table_related,
        )
        enriched = enrich_columns_with_descriptions(columns, col_fk_map)

        texts.append(text)
        descriptions.append(table_desc)
        enriched_cols.append(enriched)

    logger.info("[embed] '%s' — encoding %d text blobs with BGE-M3", db_name, len(texts))
    embeddings = embedder.embed(texts)

    logger.info("[embed] '%s' — writing embeddings to metadata DB", db_name)
    for i, row in enumerate(tables):
        _update_table_row(
            meta_conn,
            table_id         = row['id'],
            description      = descriptions[i],
            enriched_columns = enriched_cols[i],
            embedding        = embeddings[i],
        )
        logger.debug("[embed] '%s' [%3d/%d] %s", db_name, i + 1, len(tables), row['table_name'])

    logger.info(
        "[embed] '%s' — done: %d tables embedded (dim=1024, model=BAAI/bge-m3)",
        db_name, len(tables),
    )
    return len(tables)


# ── Legacy CLI ────────────────────────────────────────────────────────────────

def _run_from_env(env_path: str = DEFAULT_ENV_PATH, db_name: str | None = None) -> None:
    logger = get_logger(__name__)
    load_dotenv(env_path)

    if db_name is None:
        db_name = os.getenv("POSTGRES_DB_NAME")

    meta_conn = get_metadata_connection(env_path=env_path)
    try:
        embedder = Embedder()
        embed_one_db(meta_conn, db_name, embedder)
    finally:
        meta_conn.close()


if __name__ == "__main__":
    env = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ENV_PATH
    db  = sys.argv[2] if len(sys.argv) > 2 else None
    _run_from_env(env_path=env, db_name=db)
