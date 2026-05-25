# CLAUDE.md

Instructions and project context for Claude Code. Read this file before doing any work in this repo.

---

## 1. Project Overview

**text_to_sql** — a RAG-based natural language to SQL system for PostgreSQL databases.

Users send a question in plain English via REST API. The system:
1. Embeds the question with BGE-M3 (1024-dim).
2. Retrieves relevant tables via pgvector cosine similarity.
3. Expands schema context using FK relationships and pre-computed two-hop bridge paths.
4. Calls an LLM to generate SQL.
5. Validates SQL against a 7-rule safety policy (sqlglot).
6. Executes against the target DB.
7. Returns rows + an optional natural-language summary.

**Target DB:** ERPHUB — 165 tables, 3294 FK relationships, 1019 two-hop paths. Full DDL is too large to fit in any LLM context window — that's why Schema RAG is the core idea: only retrieve and inject the tables relevant to the user's question.

**Key innovations:**
- Pre-computed two-hop FK bridge paths (table_a → bridge → table_b)
- Multi-tenant employee-based access control via JWT (`emp_id` claim → `emp_table_access` filter)
- LLM self-correction: if the generated SQL fails, the error is fed back to the LLM for one retry

**Use case:** Enterprise BI — employees query an ERP-style PostgreSQL database without writing SQL.

---

## 2. Two-Phase Workflow

The system has a strict separation between **setup** (one-time per DB) and **runtime** (query serving).

### Phase A — Setup / Indexing (sync, psycopg2)
1. `src/run_setup.py` — connect to target DB, extract DDL + FKs, register in metadata DB, build two-hop paths.
2. `src/run_llm_descriptions.py` — generate semantic table/column descriptions via LLM.
3. `src/run_embedder.py` — generate BGE-M3 embeddings for each table description, store in metadata DB.
4. `src/run_privilege_sync.py` — fuzzy-match privilege codes to tables, populate `emp_table_access`.

### Phase B — Runtime / Query Serving (async, asyncpg, FastAPI)
- `src/api.py` — POST `/generate` orchestrates the full query pipeline.

---

## 3. Tech Stack

**Language / Runtime**
- Python 3.12
- conda env: `sql_llm`

**Web**
- FastAPI 0.115.12
- Uvicorn 0.34.0
- Pydantic 2.8.2

**Databases**
- PostgreSQL — target DB (ERPHUB) AND metadata DB (`nl2sql_metadata`)
- pgvector extension — 1024-dim cosine similarity vector search
- psycopg2-binary — sync driver (setup/indexing)
- asyncpg — async driver (FastAPI runtime)

**AI / LLM**
- Embedder: `BAAI/bge-m3` via sentence-transformers 5.3.0 — 1024-dim, runs on CPU locally
- LLM: `cyankiwi/Qwen3-30B-A3B-Instruct-2507-AWQ-4bit` via vLLM on RunPod
- OpenAI SDK 1.82.0 — OpenAI-compatible client pointed at the vLLM endpoint
- **Thinking mode is DISABLED** — always send `extra_body={"chat_template_kwargs": {"enable_thinking": False}}`

**Auth & Security**
- PyJWT 2.12.1 — HS256 Bearer token validation, extracts `emp_id`
- sqlglot 26.17.0 — SQL parse + 7-rule safety validation before executing

**Other**
- python-dotenv 1.2.2 — env loaded from `env/.env`
- Logger: `src/logger.py` — rotating file, `logs/app.log` (10MB × 5 backups)

---

## 4. sqlglot 26.17.0 Quirks (don't forget)

The validator (`src/query_pipeline/sql_validator.py`) depends on these — they were learned the hard way:
- `exp.AlterTable` does **not** exist — use `exp.Alter`.
- `exp.Revoke` does **not** exist — omit it from the DDL/DML denylist.
- `table_node.args.get("db")` returns an `Identifier` node — use `.name`:
  `db_node.name if db_node else ""`
- `func_node.sql_name` is a **bound method** — call it:
  `sql_name_attr() if callable(sql_name_attr) else sql_name_attr`

---

## 5. Repo Layout

```
text_to_sql/
├── src/
│   ├── api.py                          # FastAPI app, POST /generate
│   ├── auth.py                         # JWT validation, emp_id extraction
│   ├── logger.py                       # Rotating file logger
│   ├── metadata_store.py               # All CRUD on nl2sql_metadata DB
│   ├── run_setup.py                    # Phase 1: schema extraction + registration
│   ├── run_llm_descriptions.py         # Phase 2: LLM descriptions
│   ├── run_embedder.py                 # Phase 3: BGE-M3 embeddings
│   ├── run_privilege_sync.py           # Privilege → table fuzzy mapping
│   ├── schema_extractor/
│   │   ├── ddl_extractor.py            # DDL + column metadata
│   │   ├── fk_extractor.py             # FK relationships
│   │   ├── path_builder.py             # FK adjacency graph + two-hop paths
│   │   └── list_tables.py
│   ├── description_embedder/
│   │   ├── description_generator.py    # Structural (no-LLM) text blobs
│   │   ├── llm_describer.py            # LLM-based semantic descriptions
│   │   └── embedder.py                 # BGE-M3 loader + encode
│   └── query_pipeline/
│       ├── retriever.py                # embed → vector_search → FK expand → DDL context
│       └── sql_validator.py            # 7-rule SQL safety validation
├── tests/                              # pytest suite (54 tests)
├── migrations/                         # nl2sql_metadata schema
│   ├── 000_create_metadata_db.sql
│   ├── 001_add_embedding_column.sql
│   ├── 002_add_two_hop_paths.sql
│   └── 003_add_emp_table_access.sql
├── env/
│   ├── .env                            # Real credentials — DO NOT COMMIT
│   └── .env.temp                       # Template
├── Dockerfile
├── docker-compose.yml
├── .dockerignore
├── requirements.txt
├── README.md
└── CLAUDE.md                           # this file
```

---

## 6. API Specification

**Endpoint:** `POST /generate`

**Headers:**
```
Authorization: Bearer <JWT_TOKEN>
```

**Request body:**
```json
{
  "db_name": "ERPHUB",
  "prompt": "Show me the top 5 customers by revenue last month",
  "top_k": 5
}
```

**Response:**
```json
{
  "success": true,
  "processing_time": 2.34,
  "emp_id": 1234,
  "original_prompt": "...",
  "retrieved_tables": ["..."],
  "generated_sql": "SELECT ...",
  "retried": false,
  "data": [...],
  "answer": "..."
}
```

**JWT:** HS256. Secret in env var `JWT_SECRET_KEY`. Token must contain an `emp_id` claim.

---

## 7. Metadata DB Schema (`nl2sql_metadata`)

| Table | Purpose |
|---|---|
| `registered_databases` | db_name, host, port, schema_name, description |
| `table_metadata` | db_id (FK), schema_name, table_name, table_description, columns_info (JSONB), ddl_text, embedding vector(1024) |
| `table_relationships` | FK relationships extracted from target DB |
| `two_hop_paths` | Pre-computed bridge paths: table_a, bridge_table, table_b |
| `privilege_table_access` | privilege_id → table_name (optional RBAC) |
| `emp_table_access` | emp_id → table_name (optional RBAC) |

`columns_info` JSONB structure:
```json
{ "name": "...", "data_type": "...", "is_nullable": true, "column_default": null,
  "is_primary_key": false, "is_unique": false, "description": "..." }
```

---

## 8. Environment Variables (`env/.env`)

Copy `env/.env.temp` → `env/.env` and fill these in:

| Variable | Purpose |
|---|---|
| `DATABASE_IP`, `DATABASE_PORT`, `DATABASE_USER`, `DATABASE_DB_NAME`, `DATABASE_PASSWORD` | Target DB (ERPHUB) |
| `METADATA_DB_HOST`, `METADATA_DB_PORT`, `METADATA_DB_USER`, `METADATA_DB_NAME`, `METADATA_DB_PASSWORD` | Metadata DB |
| `LLM_BASE_URL`, `LLM_MODEL` | OpenAI-compatible LLM endpoint |
| `JWT_SECRET_KEY` | HS256 secret |
| `MIN_RELEVANCE_SCORE` | Cosine similarity threshold (default 0.5) |
| `REGISTERED_DB_CREDENTIALS` | JSON dict of per-db connection credentials |

**RunPod gotcha:** the vLLM endpoint IP changes every session. Update `LLM_BASE_URL` in `env/.env` and verify with `GET /v1/models` before running anything.

---

## 9. Running the Project

```bash
# Activate env
conda activate sql_llm

# One-time setup (in order)
python src/run_setup.py
python src/run_llm_descriptions.py
python src/run_embedder.py
python src/run_privilege_sync.py

# Run API
cd src
uvicorn api:app --host 0.0.0.0 --port 8080

# Run tests
pytest
```

Docker is also wired up — see `Dockerfile` and `docker-compose.yml`.

---

## 10. Key Architectural Patterns

- **Async/sync split:** sync psycopg2 for setup, async asyncpg for the API.
- **BGE-M3 lifecycle:** loaded once at FastAPI lifespan startup. Embedding offloaded to a thread executor (it's CPU-bound).
- **DB pools:** one connection pool per registered DB, created at lifespan startup.
- **db_name → db_id mapping:** cached at startup.
- **LLM retry:** if the generated SQL fails to execute, the error is fed back to the LLM for **one** self-correction attempt. Track this with the `retried` field in the response.
- **Relevance gate:** `MIN_RELEVANCE_SCORE` (default 0.5) rejects off-topic queries before LLM call.
- **Parallel expansion:** FK + two-hop expansion runs via `asyncio.gather`.

---

## 11. Remaining TODO (priority order)

1. Remove the `[EXEC]` debug log line from `api.py` once testing confirms it's not needed.
2. Automatic DB detection — infer `db_name` from vector search top-1 similarity and remove it from the request body.
3. Embedding cache — LRU cache on query vectors to skip re-embedding identical questions.
4. Config dataclass — centralize `top_k`, model name, thresholds, etc. into `src/config.py`.
5. Retrieval tuning — test prompts, audit `retrieved_tables` quality, consider adding a reranker.

---

## 12. Workflow Rules (READ THIS — non-negotiable)

These are user preferences that apply to **every** task in this repo.

### 12.1 Commit style
- **Single short line.**
- **Lowercase.**
- **Imperative mood** (e.g. "add", "fix", "update", "remove").
- **No period at the end.**
- **No body.**
- **No `Co-Authored-By` footer. Ever.**

Examples (good):
```
add metadata DB creation script
fix typo in ddl_extractor
update README: add JWT auth, docker section, migration 003, env vars
add docker support for api service
```

Examples (bad — do **not** do these):
```
Add metadata DB creation script.        ← capitalized, has period
feat(metadata): add metadata DB         ← conventional commits prefix not used here
add metadata script                     ← OK shape but vague

Add metadata DB creation script

This commit adds the SQL script that...
                                        ← has body, don't add one

Co-Authored-By: Claude <noreply@...>    ← never include
```

### 12.2 Commit grouping
- Commit files in **logical groups by feature/concern**, not all at once.
- If multiple files change across different concerns, **split into separate commits**.

### 12.3 Discuss before structural changes
- For new modules, renames, or major refactors: **propose first, wait for approval, then code.**
- Small bug fixes and additions inside an existing file are fine to just do.

### 12.4 Test before committing
- **Run and test code before committing. Never commit untested code.**
- Run the relevant pytest tests, or manually verify behavior, before staging.

### 12.5 Communication style
- Short, direct answers. No filler. No long preambles. No closing summaries unless something genuinely needs explaining.

---

## 13. Things to Never Commit

- `env/.env` — real credentials. Only `.env.temp` is safe to commit.
- `logs/` — runtime logs.
- `__pycache__/`, `*.pyc`.
- `.vscode/settings.json` (personal IDE config) — judgment call; check with the user if unsure.
- Anything in `model_comparison.xlsx` should be committed only if the user explicitly asked.

Always check `git status` and `git diff --staged` before committing. Never use `git add -A` or `git add .` blindly — stage files by name.
