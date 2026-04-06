"""
Runtime schema retriever for Phase 5.

Given a user question (already embedded as a vector), retrieves the most
relevant tables from the metadata DB and builds a DDL context string for
the LLM prompt.

Pipeline:
  1. vector_search()        — cosine similarity against table embeddings, top-K
  2. expand_fk_neighbors()  — add directly FK-connected tables to the candidate set
  3. load_tables_by_name()  — fetch full metadata for the expanded set
  4. build_ddl_context()    — format DDL + descriptions into a prompt-ready string
"""
import asyncio
import json


# ── Vector search ─────────────────────────────────────────────────────────────

async def vector_search(
    meta_pool,
    db_name: str,
    query_vector: list[float],
    top_k: int = 5,
) -> list[dict]:
    """
    Return the top-K most similar tables for the given query vector.
    Uses pgvector cosine distance (<=>).
    """
    vector_str = "[" + ",".join(f"{v:.8f}" for v in query_vector) + "]"

    query = """
        SELECT
            tm.table_name,
            tm.ddl_text,
            tm.table_description,
            tm.columns_info,
            1 - (tm.embedding <=> $1::vector) AS similarity
        FROM table_metadata tm
        JOIN registered_databases rd ON rd.id = tm.db_id
        WHERE rd.db_name = $2
          AND tm.embedding IS NOT NULL
        ORDER BY tm.embedding <=> $1::vector
        LIMIT $3;
    """
    async with meta_pool.acquire() as conn:
        rows = await conn.fetch(query, vector_str, db_name, top_k)
    return [dict(r) for r in rows]


# ── FK expansion ──────────────────────────────────────────────────────────────

async def expand_fk_neighbors(
    meta_pool,
    db_name: str,
    table_names: list[str],
) -> set[str]:
    """
    Return all tables that are directly FK-connected (in either direction)
    to any table in table_names.
    """
    query = """
        SELECT DISTINCT tr.target_table AS neighbor
        FROM table_relationships tr
        JOIN registered_databases rd ON rd.id = tr.db_id
        WHERE rd.db_name = $1
          AND tr.source_table = ANY($2)
        UNION
        SELECT DISTINCT tr.source_table AS neighbor
        FROM table_relationships tr
        JOIN registered_databases rd ON rd.id = tr.db_id
        WHERE rd.db_name = $1
          AND tr.target_table = ANY($2);
    """
    async with meta_pool.acquire() as conn:
        rows = await conn.fetch(query, db_name, table_names)
    return {r["neighbor"] for r in rows}


# ── Two-hop bridge expansion ─────────────────────────────────────────────────

async def expand_two_hop_bridges(
    meta_pool,
    db_name: str,
    table_names: list[str],
) -> set[str]:
    """
    For each table in table_names, find bridge tables from pre-computed
    two-hop paths where that table appears as table_a or table_b.

    Example: if 'employee' and 'orders' are retrieved, and the path
    employee ──[contracts]── orders exists, 'contracts' is returned.
    """
    query = """
        SELECT DISTINCT thp.bridge_table
        FROM two_hop_paths thp
        JOIN registered_databases rd ON rd.id = thp.db_id
        WHERE rd.db_name = $1
          AND (thp.table_a = ANY($2) OR thp.table_b = ANY($2));
    """
    async with meta_pool.acquire() as conn:
        rows = await conn.fetch(query, db_name, table_names)
    return {r["bridge_table"] for r in rows}


# ── Load metadata for a specific set of tables ───────────────────────────────

async def load_tables_by_name(
    meta_pool,
    db_name: str,
    table_names: list[str],
) -> list[dict]:
    """
    Fetch full metadata (DDL, description, columns) for a given list of tables.
    """
    query = """
        SELECT
            tm.table_name,
            tm.ddl_text,
            tm.table_description,
            tm.columns_info
        FROM table_metadata tm
        JOIN registered_databases rd ON rd.id = tm.db_id
        WHERE rd.db_name = $1
          AND tm.table_name = ANY($2)
        ORDER BY tm.table_name;
    """
    async with meta_pool.acquire() as conn:
        rows = await conn.fetch(query, db_name, table_names)
    return [dict(r) for r in rows]


# ── DDL context builder ───────────────────────────────────────────────────────

def build_ddl_context(tables: list[dict]) -> str:
    """
    Format retrieved tables into a DDL context string for the LLM prompt.

    Each table block includes:
      - Table description
      - Column descriptions (from columns_info)
      - Raw DDL text
    """
    blocks = []
    for t in tables:
        lines = []

        if t.get("table_description"):
            lines.append(f"-- {t['table_description']}")

        col_info = t.get("columns_info") or []
        if isinstance(col_info, str):
            col_info = json.loads(col_info)
        if col_info:
            lines.append("-- Columns:")
            for col in col_info:
                desc = col.get("description", "")
                desc_str = f"  -- {desc}" if desc else ""
                lines.append(f"--   {col['name']} ({col['data_type']}){desc_str}")

        lines.append(t["ddl_text"])
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)


# ── Full retrieval pipeline ───────────────────────────────────────────────────

async def retrieve_context(
    meta_pool,
    embedder,
    db_name: str,
    user_question: str,
    top_k: int = 5,
) -> tuple[str, list[str]]:
    """
    End-to-end retrieval: embed question → vector search → FK expand → build context.

    Returns:
        ddl_context   — formatted string ready for the LLM system prompt
        table_names   — list of all table names included in the context
    """
    # 1. Embed the user question (offloaded to thread pool — BGE-M3 is CPU-bound)
    loop = asyncio.get_event_loop()
    vectors = await loop.run_in_executor(None, embedder.embed, [user_question])
    query_vector = vectors[0]

    # 2. Vector search — top-K most similar tables
    top_tables = await vector_search(meta_pool, db_name, query_vector, top_k=top_k)
    top_names = [t["table_name"] for t in top_tables]

    # 3. FK expansion — add direct neighbors
    neighbors = await expand_fk_neighbors(meta_pool, db_name, top_names)

    # 4. Two-hop bridge expansion — add bridge tables connecting retrieved tables
    bridges = await expand_two_hop_bridges(meta_pool, db_name, top_names)

    # 5. Load full metadata for all extra tables
    extra_names = list((neighbors | bridges) - set(top_names))
    extra_tables = []
    if extra_names:
        extra_tables = await load_tables_by_name(meta_pool, db_name, extra_names)

    # 6. Build context — top tables first (most relevant), extras after
    all_tables = top_tables + extra_tables
    ddl_context = build_ddl_context(all_tables)
    all_names = [t["table_name"] for t in all_tables]

    return ddl_context, all_names
