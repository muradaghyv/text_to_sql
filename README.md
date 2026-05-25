# text-to-SQL

A RAG-based text-to-SQL system for PostgreSQL. You connect it to one or more databases, it reads each schema once and embeds it, and then lets users ask questions in plain language. The system finds the relevant tables, builds a DDL context, asks an LLM to write the query, validates the SQL with `sqlglot`, and executes it. Per-employee table access is enforced via JWT.

The system is deliberately database-agnostic. The only thing tying it to a specific schema is what an admin inserts into the `registered_databases` table.

---

## How it works

```
                      ┌────────────────────────────┐
Admin (admin panel    │  registered_databases      │
or psql) inserts a ──▶│  one row per target DB:    │
target DB row         │  host, port, user, pwd*    │
                      └─────────────┬──────────────┘
                                    │
                          docker compose up
                                    ▼
                ┌──────────────────────────────────────┐
                │  Container startup pipeline           │
                │                                       │
                │  1. ensure metadata DB + role exist  │
                │  2. apply migrations (idempotent)     │
                │  3. for every unindexed registered_   │
                │     databases row:                    │
                │       a. extract DDL, FKs, two-hop    │
                │          paths from target DB         │
                │       b. pull pg_description comments │
                │       c. embed with BGE-M3            │
                │       d. write to metadata DB         │
                │     (failures recorded, others        │
                │      continue)                        │
                │  4. start uvicorn                     │
                └──────────────────────────────────────┘
                                    │
                  POST /generate    ▼
                ┌──────────────────────────────────────┐
                │  question → embed → vector search →   │
                │  FK + two-hop expansion → DDL context │
                │  → LLM writes SQL → sqlglot validates │
                │  → execute against target DB → return │
                └──────────────────────────────────────┘

* Passwords are stored encrypted with pgcrypto symmetric encryption.
  The encryption key is read from the DB_CRED_ENCRYPTION_KEY env var.
```

---

## Prerequisites

- Docker and Docker Compose
- A PostgreSQL server reachable from the container — this hosts the metadata DB. It must have `pgvector` and `pgcrypto` extensions installed (Ubuntu: `apt install postgresql-16-pgvector` plus pgcrypto, which ships with PostgreSQL).
- One or more target PostgreSQL databases the system will be allowed to query.
- An OpenAI-compatible LLM endpoint (e.g. vLLM, Ollama).
- ~2 GB disk for the BGE-M3 embedding model (downloaded automatically on first start).

---

## First-time setup (one-time)

### 1. Configure `env/.env`

Copy the template and fill in the four blocks:

```bash
cp env/.env.temp env/.env
```

```env
# LLM endpoint
LLM_BASE_URL=http://host.docker.internal:8000/v1
LLM_MODEL=cyankiwi/Qwen3-30B-A3B-Instruct-2507-AWQ-4bit

# Metadata DB — application user (auto-created on first run if missing)
METADATA_DB_HOST=your-metadata-db-host
METADATA_DB_PORT=5432
METADATA_DB_USER=nl2sql_user
METADATA_DB_NAME=nl2sql_metadata
METADATA_DB_PASSWORD=a-strong-password

# Metadata DB superuser — used on startup to create the DB + role and apply
# migrations (CREATE EXTENSION vector / pgcrypto require superuser).
METADATA_DB_ADMIN_USER=postgres
METADATA_DB_ADMIN_PASSWORD=your-postgres-superuser-password

# Symmetric key used to encrypt/decrypt target-DB passwords stored in
# registered_databases. Must be the same value used when inserting rows.
# Pick a long random value and keep it secret.
DB_CRED_ENCRYPTION_KEY=a-long-random-string

# JWT — must match the key used by the auth system that signs tokens
JWT_SECRET_KEY=your-jwt-secret

# Off-topic rejection floor (cosine similarity, default 0.5)
MIN_RELEVANCE_SCORE=0.5
```

> `host.docker.internal` resolves to your host machine from inside the container; use it for any service running on your laptop (e.g. vLLM).

### 2. First docker run — creates the metadata DB

```bash
docker compose --env-file env/.env up
```

The container will:
- create `nl2sql_user` if missing,
- create the `nl2sql_metadata` database if missing,
- apply every migration in `migrations/`,
- detect that `registered_databases` is empty and **exit with a message** telling you to populate it.

You should see something like:

```
────────────────────────────────────────────────────────────────────────
Metadata DB 'nl2sql_metadata' is ready, but no target DBs are registered.
Insert one row per target DB into the registered_databases table and
re-run `docker compose up`.
────────────────────────────────────────────────────────────────────────
```

### 3. Register a target DB

Connect to the metadata DB and insert one row per target database. The password is encrypted with `pgp_sym_encrypt` using your `DB_CRED_ENCRYPTION_KEY`:

```sql
INSERT INTO registered_databases
    (db_name, host, port, schema_name, db_user, db_password_encrypted)
VALUES (
    'ERPHUB',
    'your-target-db-host',
    5432,
    'public',
    'erphub_reader',
    pgp_sym_encrypt('your-target-db-password', 'a-long-random-string')
);
```

> The same `DB_CRED_ENCRYPTION_KEY` value goes in both places — `env/.env` and the `pgp_sym_encrypt` call. If they don't match, the API will fail to decrypt at startup.

### 4. Second docker run — indexes the target DB and starts the API

```bash
docker compose --env-file env/.env up -d
```

This time the container detects the new `registered_databases` row, indexes it (DDL → FKs → two-hop paths → embeddings), then starts uvicorn on port 8080. Watch progress with:

```bash
docker compose logs -f api
```

You're ready when you see `INFO: Application startup complete.`

---

## Adding a second target database

Insert another row into `registered_databases` (with a different `db_name`), then restart:

```sql
INSERT INTO registered_databases
    (db_name, host, port, schema_name, db_user, db_password_encrypted)
VALUES (
    'ABC',
    'abc-db-host',
    5432,
    'public',
    'abc_reader',
    pgp_sym_encrypt('abc-password', 'a-long-random-string')
);
```

```bash
docker compose --env-file env/.env down
docker compose --env-file env/.env up -d
```

The container scans `registered_databases`, finds that ABC has no rows in `table_metadata`, and indexes only ABC. Existing DBs (e.g. ERPHUB) are untouched. After startup, both DBs serve `/generate`:

```json
{ "db_name": "ERPHUB", "prompt": "..." }
{ "db_name": "ABC",    "prompt": "..." }
```

If indexing of one row fails (e.g. unreachable host), the error is written to its `indexing_error` column and the API starts anyway with whatever indexed successfully. Fix the row and restart to retry; the indexer will pick it up because of the non-null `indexing_error`.

---

## Optional — LLM-generated descriptions

By default the indexer only pulls table/column comments that are already on the target DB (`COMMENT ON TABLE`, `COMMENT ON COLUMN`). If your target DB has no comments, retrieval still works (a structural fallback like "Stores employee records" is used) but quality is worse.

To enrich descriptions with an LLM, run the `describe` mode after the API is up:

```bash
docker compose --env-file env/.env run --rm api describe              # all DBs, az, fill-empty
docker compose --env-file env/.env run --rm api describe --force      # overwrite everything
docker compose --env-file env/.env run --rm api describe --db ERPHUB --lang en
```

Defaults: `--lang az` (Azerbaijani output) and **fill-empty** — only tables/columns whose description is currently empty get an LLM call. Use `--force` to overwrite everything. After updates the affected tables are re-embedded automatically.

---

## API usage

**POST** `/generate`

```bash
curl -X POST http://localhost:8080/generate \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <JWT>" \
  -d '{"db_name": "ERPHUB", "prompt": "Show all orders placed by customers in Baku"}'
```

JWT must be HS256-signed with `JWT_SECRET_KEY` and contain an `emp_id` claim. The API uses `emp_id` to enforce per-employee table access — only tables the employee has been granted access to are included in the LLM context. Grants live in `emp_table_access`. If `emp_table_access` has no rows for an `emp_id`, the API returns 403.

Response shape:

```json
{
  "success": true,
  "processing_time": 1.43,
  "emp_id": 42,
  "original_prompt": "...",
  "retrieved_tables": ["orders", "customers"],
  "generated_sql": "SELECT ...",
  "retried": false,
  "data": [...],
  "answer": "..."
}
```

`retried` is true if the first SQL attempt failed at execution and the LLM was given the error for one self-correction attempt.

### Safety features

1. **Off-topic rejection.** If the question's top cosine similarity to any table embedding is below `MIN_RELEVANCE_SCORE` (default 0.5), the API returns 422 — the LLM is never called.
2. **SQL validation (sqlglot, 7 rules).** The generated SQL must parse cleanly, be a single `SELECT`, reference only employee-allowed tables, and pass other checks (no DDL/DML, no system tables, etc.).
3. **Self-correction retry.** If a validated query fails at execution time, the error is fed back to the LLM for one retry.

---

## Testing without a second target DB

For testing the multi-DB flow, the simplest option is to clone an existing target DB on the same Postgres server. As superuser, with no active connections to the source DB:

```sql
CREATE DATABASE "ERPHUB_NEW" WITH TEMPLATE "ERPHUB" OWNER <owner>;
```

Then insert a second row in `registered_databases` pointing at `ERPHUB_NEW` and restart docker. Indexing is independent for each DB so this gives you a real two-DB setup without provisioning new data.

---

## Common operations

**Change `env/.env` and reload:**

```bash
docker compose --env-file env/.env down
# edit env/.env
docker compose --env-file env/.env up -d
```

`down` removes the container; `up` recreates with the new env. Plain `restart` does **not** pick up new env values.

**Rebuild after changing source code, `Dockerfile`, or `requirements.txt`:**

```bash
docker compose --env-file env/.env up -d --build
```

**Re-index a target DB** (schema changed) — clear its child rows so the orchestrator treats it as unindexed:

```sql
DELETE FROM table_metadata     WHERE db_id = (SELECT id FROM registered_databases WHERE db_name='ERPHUB');
DELETE FROM table_relationships WHERE db_id = (SELECT id FROM registered_databases WHERE db_name='ERPHUB');
DELETE FROM two_hop_paths      WHERE db_id = (SELECT id FROM registered_databases WHERE db_name='ERPHUB');
```

```bash
docker compose --env-file env/.env down
docker compose --env-file env/.env up -d
```

**Stop:**

```bash
docker compose --env-file env/.env down
```

**Wipe the BGE-M3 cache** (forces a re-download next time):

```bash
docker compose --env-file env/.env down -v
```

This does **not** touch your metadata DB.

---

## Per-employee table access

The API enforces table-level access control: a JWT with `emp_id=42` can only query tables that employee 42 has been granted access to. Grants are rows in `emp_table_access`:

```sql
INSERT INTO emp_table_access (emp_id, db_id, table_name) VALUES
  (42, 1, 'orders'),
  (42, 1, 'customers');
```

For ERPHUB-style target DBs (with `privileges`, `emp_roles`, `role_privileges` tables) there is also a fuzzy auto-sync script:

```bash
docker compose --env-file env/.env run --rm api python run_privilege_sync.py --apply
```

> **Note:** `run_privilege_sync.py` is currently still hard-coded to the legacy `POSTGRES_*` env vars. If you need it for a non-ERPHUB target DB, populate `emp_table_access` manually with INSERTs as shown above.

---

## Local development (without Docker)

```bash
conda create -n sql_llm python=3.12 -y
conda activate sql_llm
pip install -r requirements.txt
```

The legacy CLI scripts in `src/` still work for ad-hoc development against a single target DB, using `POSTGRES_*` env vars:

```bash
python src/run_setup.py                                          # extract schema
python src/run_embedder.py                                       # embed
python src/run_llm_descriptions.py --db ERPHUB --lang az         # optional descriptions
```

These bypass the multi-DB orchestrator and are mostly useful when iterating on a single DB.

---

## Running tests

```bash
conda activate sql_llm
python -m pytest tests/ -v
```

61 unit tests; no DB connection required.

---

## Repo layout

```
ai_erp_report/
├── src/
│   ├── api.py                        # FastAPI app, POST /generate
│   ├── auth.py                       # JWT validation
│   ├── logger.py
│   ├── metadata_store.py             # all CRUD on nl2sql_metadata
│   ├── run_index_unindexed.py        # startup orchestrator
│   ├── run_setup.py                  # per-DB schema extraction
│   ├── run_embedder.py               # per-DB BGE-M3 embedding
│   ├── run_llm_descriptions.py       # describe mode (optional)
│   ├── run_privilege_sync.py         # legacy ERPHUB privilege sync
│   ├── schema_extractor/             # DDL, FK, two-hop path extraction
│   ├── description_embedder/         # text-blob builder + embedder
│   └── query_pipeline/               # retriever, sql_validator
├── migrations/                       # 000–004, all idempotent
├── env/.env.temp                     # template
├── docker-entrypoint.sh
├── docker-compose.yml
├── Dockerfile
└── tests/                            # pytest, mocks only
```

---

## Stack

- Python 3.12, FastAPI, asyncpg (runtime) + psycopg2 (indexing)
- PostgreSQL with `pgvector` and `pgcrypto` extensions
- BAAI/bge-m3 1024-dim embeddings via sentence-transformers (CPU)
- Qwen3 via vLLM (or any OpenAI-compatible endpoint) for SQL generation and descriptions
- sqlglot 26.x for SQL safety validation (7 rules)
- PyJWT for HS256 token validation
