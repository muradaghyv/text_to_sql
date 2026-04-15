import re
import time
import json
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import AsyncOpenAI
from dotenv import load_dotenv

from description_embedder.embedder import Embedder
from logger import get_logger
from query_pipeline.retriever import retrieve_context, embed_question, vector_search
from query_pipeline.sql_validator import SQLValidationError, validate_sql

logger = get_logger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

load_dotenv(ENV_PATH)

# Prevent tokenizers from spawning parallel workers — avoids semaphore leaks on shutdown
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

LLM_BASE_URL = os.getenv("LLM_BASE_URL")
LLM_MODEL    = os.getenv("LLM_MODEL")

META_HOST     = os.getenv("METADATA_DB_HOST")
META_PORT     = os.getenv("METADATA_DB_PORT", "5432")
META_USER     = os.getenv("METADATA_DB_USER")
META_DB_NAME  = os.getenv("METADATA_DB_NAME")
META_PASSWORD = os.getenv("METADATA_DB_PASSWORD")

for key, val in [
    ("LLM_BASE_URL", LLM_BASE_URL), ("LLM_MODEL", LLM_MODEL),
    ("METADATA_DB_HOST", META_HOST), ("METADATA_DB_USER", META_USER),
    ("METADATA_DB_NAME", META_DB_NAME), ("METADATA_DB_PASSWORD", META_PASSWORD),
]:
    if not val:
        raise ValueError(f"Missing required env var: {key}")

_raw_creds = os.getenv("REGISTERED_DB_CREDENTIALS")
if not _raw_creds:
    raise ValueError("Missing required env var: REGISTERED_DB_CREDENTIALS")
try:
    REGISTERED_DB_CREDENTIALS: dict = json.loads(_raw_creds)
except json.JSONDecodeError as e:
    raise ValueError(f"REGISTERED_DB_CREDENTIALS is not valid JSON: {e}")

llm_client = AsyncOpenAI(base_url=LLM_BASE_URL, api_key="EMPTY")

# Minimum cosine similarity between the question and any table embedding.
# Questions scoring below this are considered off-topic and rejected.
MIN_RELEVANCE_SCORE = float(os.getenv("MIN_RELEVANCE_SCORE", "0.5"))


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting up...")

    # One asyncpg pool per registered DB
    app.state.db_pools = {}
    for db_name, creds in REGISTERED_DB_CREDENTIALS.items():
        app.state.db_pools[db_name] = await asyncpg.create_pool(
            host=creds["host"],
            port=creds.get("port", 5432),
            user=creds["user"],
            password=creds["password"],
            database=db_name,
        )
        logger.info("Target DB pool ready (%s)", db_name)

    # Metadata DB pool
    app.state.meta_pool = await asyncpg.create_pool(
        host=META_HOST, port=META_PORT,
        user=META_USER, password=META_PASSWORD, database=META_DB_NAME,
    )
    logger.info("Metadata DB pool ready (%s)", META_DB_NAME)

    # Cache db_name → db_id mapping from registered_databases
    async with app.state.meta_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, db_name FROM registered_databases")
    app.state.db_ids = {r["db_name"]: r["id"] for r in rows}
    logger.info("Loaded db_id map: %s", app.state.db_ids)

    # Embedder (loads BGE-M3 once)
    logger.info("Loading BGE-M3 embedder...")
    app.state.embedder = Embedder()
    logger.info("Embedder ready.")

    yield

    for db_name, pool in app.state.db_pools.items():
        try:
            await pool.close()
        except Exception as e:
            logger.warning("Error closing pool for %s: %s", db_name, e)
    try:
        await app.state.meta_pool.close()
    except Exception as e:
        logger.warning("Error closing metadata pool: %s", e)
    await llm_client.close()
    logger.info("Shutdown complete.")


app = FastAPI(lifespan=lifespan)


# ── Request / response models ─────────────────────────────────────────────────

class UserRequest(BaseModel):
    emp_id: int        # employee ID — used to enforce table-level access control
    db_name: str       # must match a db_name in registered_databases
    prompt: str
    top_k: int = 5     # number of tables to retrieve via vector search


# ── Helpers ───────────────────────────────────────────────────────────────────

def format_sql(text: str) -> str:
    text = re.sub(r"```sql|```", "", text, flags=re.IGNORECASE).strip()
    if not text.endswith(";"):
        text += ";"
    return text


async def _call_llm(messages: list[dict], temperature: float = 0.1) -> str:
    """Raw LLM call — returns content string as-is, raises HTTPException on error."""
    try:
        response = await llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=temperature,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    except Exception as e:
        logger.error("LLM error: %s", e, exc_info=True)
        raise HTTPException(status_code=503, detail=f"LLM error: {e}")
    logger.debug("[TOKENS] prompt=%d  completion=%d",
                 response.usage.prompt_tokens, response.usage.completion_tokens)
    return response.choices[0].message.content or ""


async def call_llm(messages: list[dict]) -> str:
    return format_sql(await _call_llm(messages))


AUGMENT_ROW_LIMIT = 50


async def augment_answer(question: str, sql: str, data: list[dict]) -> str | None:
    """Ask the LLM to summarise query results in natural language. Returns None on failure."""
    rows = data[:AUGMENT_ROW_LIMIT]
    truncated = len(data) > AUGMENT_ROW_LIMIT
    data_text = json.dumps(rows, ensure_ascii=False, default=str)
    if truncated:
        data_text += f"\n\n(Showing first {AUGMENT_ROW_LIMIT} of {len(data)} rows)"

    messages = [
        {
            "role": "system",
            "content": (
                "You are a helpful data assistant. "
                "The user asked a question and the system ran a SQL query to answer it. "
                "Your job is to summarise the query results in clear, natural language. "
                "Be concise. Do not repeat the SQL. Do not add information not present in the data."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"SQL used:\n{sql}\n\n"
                f"Results:\n{data_text}"
            ),
        },
    ]
    try:
        return await _call_llm(messages, temperature=0.3)
    except Exception as e:
        logger.warning("Answer augmentation failed: %s", e)
        return None


async def get_allowed_tables(meta_pool, emp_id: int, db_id: int) -> set[str]:
    """Return the set of table names this employee is allowed to read."""
    query = """
        SELECT table_name
        FROM emp_table_access
        WHERE emp_id = $1 AND db_id = $2;
    """
    async with meta_pool.acquire() as conn:
        rows = await conn.fetch(query, emp_id, db_id)
    return {r["table_name"] for r in rows}


async def execute_query(pool, sql: str) -> list[dict]:
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(sql)
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error("Database error: %s | sql=%s", e, sql, exc_info=True)
            raise HTTPException(status_code=500, detail=f"Database error: {e}")


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.post("/generate")
async def generate_answer(user_request: UserRequest):
    t_start = time.time()

    db_name = user_request.db_name
    emp_id  = user_request.emp_id

    if db_name not in app.state.db_pools:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown db_name '{db_name}'. Registered: {list(app.state.db_pools.keys())}",
        )

    # 1. Resolve employee's allowed tables
    db_id = app.state.db_ids.get(db_name)
    allowed_tables = await get_allowed_tables(app.state.meta_pool, emp_id, db_id)
    if not allowed_tables:
        logger.warning("Access denied — emp_id=%d has no table access for db=%s", emp_id, db_name)
        raise HTTPException(status_code=403, detail=f"Employee {emp_id} has no table access for database '{db_name}'")
    logger.info("emp_id=%d allowed_tables=%s", emp_id, sorted(allowed_tables))

    # 2. Embed question once — reused for relevance check and retrieval
    query_vector = await embed_question(app.state.embedder, user_request.prompt)

    # 4. Vector search — one call serves both relevance check and retrieval
    top_tables = await vector_search(app.state.meta_pool, db_name, query_vector, top_k=user_request.top_k)
    top_similarity = top_tables[0]["similarity"] if top_tables else 0.0
    if top_similarity < MIN_RELEVANCE_SCORE:
        logger.warning("Off-topic query rejected — emp_id=%d score=%.3f prompt=%r",
                       emp_id, top_similarity, user_request.prompt)
        raise HTTPException(
            status_code=400,
            detail="Query does not appear to be related to the database. Please ask a data retrieval question.",
        )
    logger.info("Relevance check passed — score=%.3f prompt=%r", top_similarity, user_request.prompt)

    # 5. Retrieve relevant schema context (top_tables pre-fetched — skips second vector search)
    try:
        ddl_context, table_names = await retrieve_context(
            meta_pool=app.state.meta_pool,
            embedder=app.state.embedder,
            db_name=db_name,
            user_question=user_request.prompt,
            top_k=user_request.top_k,
            allowed_tables=allowed_tables,
            query_vector=query_vector,
            top_tables=top_tables,
        )
    except Exception as e:
        logger.error("Retrieval error: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Retrieval error: {e}")

    if not table_names:
        logger.warning("Access denied — emp_id=%d: none of the retrieved tables are allowed", emp_id)
        raise HTTPException(status_code=403, detail=f"Employee {emp_id} does not have access to the tables needed for this query")

    logger.info("Retrieved tables (after access filter): %s", table_names)

    # 6. Build system prompt and generate SQL
    system_prompt = f"""You are an expert PostgreSQL query generator.
Your job is to generate a SQL query that fetches exactly what the user asks for.

Here is the relevant database schema:

{ddl_context}

Output rules (MANDATORY):
- Output must be plain SQL only — no markdown, no explanation.
- Do NOT use ``` or ```sql.
- The first word MUST be SELECT, INSERT, UPDATE, or DELETE.
- The last character MUST be a semicolon (;).
- Return NOTHING except the SQL query itself.

EXAMPLE:
User: "Show Mirzə Abbaszadə's registered address and LinkedIn."
Output: SELECT reg_addr, linkedin FROM employee WHERE first_name = 'Mirzə' AND last_name = 'Abbaszadə';"""

    full_prompt = system_prompt + "\n\nUser: " + user_request.prompt
    logger.debug("[PROMPT] chars=%d  tokens≈%d", len(full_prompt), len(full_prompt) // 4)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_request.prompt},
    ]

    # 7. Generate SQL
    sql = await call_llm(messages)

    # 8. Validate SQL before touching the target DB
    try:
        validate_sql(sql, allowed_tables=table_names)
    except SQLValidationError as e:
        logger.warning("SQL validation failed: %s | sql=%s", e, sql)
        raise HTTPException(status_code=400, detail=f"SQL validation failed: {e}")

    # 9. Execute — retry once on failure
    logger.info("[EXEC] db_name=%s | sql=%s", db_name, sql)
    db_pool = app.state.db_pools[db_name]
    retried = False
    try:
        data = await execute_query(db_pool, sql)
    except HTTPException as first_err:
        logger.warning("[RETRY] SQL execution failed: %s | sql=%s — retrying with error context",
                       first_err.detail, sql)
        retry_messages = messages + [
            {"role": "assistant", "content": sql},
            {"role": "user",      "content": (
                f"That query failed with this error:\n{first_err.detail}\n\n"
                "Fix the SQL and return only the corrected query."
            )},
        ]
        sql = await call_llm(retry_messages)
        try:
            validate_sql(sql, allowed_tables=table_names)
        except SQLValidationError as e:
            logger.error("SQL validation failed on retry: %s | sql=%s", e, sql)
            raise HTTPException(status_code=400, detail=f"SQL validation failed on retry: {e}")
        data = await execute_query(db_pool, sql)
        retried = True
        logger.info("[RETRY] Retry succeeded.")

    # 10. Augment: summarise raw results in natural language
    answer = await augment_answer(user_request.prompt, sql, data)

    logger.info("Request completed in %.2fs | retried=%s | sql=%s",
                time.time() - t_start, retried, sql)

    return {
        "success":          True,
        "processing_time":  round(time.time() - t_start, 2),
        "emp_id":           emp_id,
        "original_prompt":  user_request.prompt,
        "retrieved_tables": table_names,
        "generated_sql":    sql,
        "retried":          retried,
        "data":             data,
        "answer":           answer,
    }
