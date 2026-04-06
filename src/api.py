import re
import time
import json
import os
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import OpenAI
from dotenv import load_dotenv

from description_embedder.embedder import Embedder
from query_pipeline.retriever import retrieve_context
from query_pipeline.sql_validator import SQLValidationError, validate_sql

# ── Config ────────────────────────────────────────────────────────────────────

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

load_dotenv(ENV_PATH)

LLM_BASE_URL = os.getenv("LLM_BASE_URL")
LLM_MODEL    = os.getenv("LLM_MODEL")

DB_HOST     = os.getenv("DATABASE_IP")
DB_PORT     = os.getenv("DATABASE_PORT", "5432")
DB_USER     = os.getenv("POSTGRES_USER")
DB_NAME     = os.getenv("POSTGRES_DB_NAME")
DB_PASSWORD = os.getenv("POSTGRES_PASSWORD")

META_HOST     = os.getenv("METADATA_DB_HOST")
META_PORT     = os.getenv("METADATA_DB_PORT", "5432")
META_USER     = os.getenv("METADATA_DB_USER")
META_DB_NAME  = os.getenv("METADATA_DB_NAME")
META_PASSWORD = os.getenv("METADATA_DB_PASSWORD")

for key, val in [
    ("LLM_BASE_URL", LLM_BASE_URL), ("LLM_MODEL", LLM_MODEL),
    ("DATABASE_IP", DB_HOST), ("POSTGRES_USER", DB_USER),
    ("POSTGRES_DB_NAME", DB_NAME), ("POSTGRES_PASSWORD", DB_PASSWORD),
    ("METADATA_DB_HOST", META_HOST), ("METADATA_DB_USER", META_USER),
    ("METADATA_DB_NAME", META_DB_NAME), ("METADATA_DB_PASSWORD", META_PASSWORD),
]:
    if not val:
        raise ValueError(f"Missing required env var: {key}")

llm_client = OpenAI(base_url=LLM_BASE_URL, api_key="EMPTY")


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("Starting up...")

    # Target DB pool
    app.state.db_pool = await asyncpg.create_pool(
        host=DB_HOST, port=DB_PORT,
        user=DB_USER, password=DB_PASSWORD, database=DB_NAME,
    )
    print(f"  Target DB pool ready ({DB_NAME})")

    # Metadata DB pool
    app.state.meta_pool = await asyncpg.create_pool(
        host=META_HOST, port=META_PORT,
        user=META_USER, password=META_PASSWORD, database=META_DB_NAME,
    )
    print(f"  Metadata DB pool ready ({META_DB_NAME})")

    # Embedder (loads BGE-M3 once)
    print("  Loading BGE-M3 embedder...")
    app.state.embedder = Embedder()
    print("  Embedder ready.")

    yield

    await app.state.db_pool.close()
    await app.state.meta_pool.close()
    print("Shutdown complete.")


app = FastAPI(lifespan=lifespan)


# ── Request / response models ─────────────────────────────────────────────────

class UserRequest(BaseModel):
    prompt: str
    top_k: int = 5   # number of tables to retrieve via vector search


# ── Helpers ───────────────────────────────────────────────────────────────────

def format_sql(text: str) -> str:
    text = re.sub(r"```sql|```", "", text, flags=re.IGNORECASE).strip()
    if not text.endswith(";"):
        text += ";"
    return text


def call_llm(messages: list[dict]) -> str:
    try:
        response = llm_client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.1,
            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
        )
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"LLM error: {e}")
    print(f"[TOKENS] prompt={response.usage.prompt_tokens}  completion={response.usage.completion_tokens}")
    return format_sql(response.choices[0].message.content or "")


async def execute_query(pool, sql: str) -> list[dict]:
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(sql)
            return [dict(r) for r in rows]
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Database error: {e}")


# ── Endpoint ──────────────────────────────────────────────────────────────────

@app.post("/generate")
async def generate_answer(user_request: UserRequest):
    t_start = time.time()

    # 1. Retrieve relevant schema context
    try:
        ddl_context, table_names = await retrieve_context(
            meta_pool=app.state.meta_pool,
            embedder=app.state.embedder,
            db_name=DB_NAME,
            user_question=user_request.prompt,
            top_k=user_request.top_k,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Retrieval error: {e}")

    print(f"Retrieved tables: {table_names}")

    # 2. Build system prompt and generate SQL
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
    print(f"[PROMPT] chars={len(full_prompt)}  tokens≈{len(full_prompt)//4}")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user",   "content": user_request.prompt},
    ]

    # 2. Generate SQL
    sql = call_llm(messages)

    # 3. Validate SQL before touching the target DB
    try:
        validate_sql(sql, allowed_tables=table_names)
    except SQLValidationError as e:
        raise HTTPException(status_code=400, detail=f"SQL validation failed: {e}")

    # 4. Execute — retry once on failure
    retried = False
    try:
        data = await execute_query(app.state.db_pool, sql)
    except HTTPException as first_err:
        print(f"[RETRY] SQL execution failed: {first_err.detail}. Retrying with error context...")
        retry_messages = messages + [
            {"role": "assistant", "content": sql},
            {"role": "user",      "content": (
                f"That query failed with this error:\n{first_err.detail}\n\n"
                "Fix the SQL and return only the corrected query."
            )},
        ]
        sql = call_llm(retry_messages)
        try:
            validate_sql(sql, allowed_tables=table_names)
        except SQLValidationError as e:
            raise HTTPException(status_code=400, detail=f"SQL validation failed on retry: {e}")
        data = await execute_query(app.state.db_pool, sql)
        retried = True

    return {
        "success":          True,
        "processing_time":  round(time.time() - t_start, 2),
        "original_prompt":  user_request.prompt,
        "retrieved_tables": table_names,
        "generated_sql":    sql,
        "retried":          retried,
        "data":             data,
    }
