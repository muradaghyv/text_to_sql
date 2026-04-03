# text-to-SQL

A RAG-based text-to-SQL system for PostgreSQL. You connect it to your database, it reads the schema once, embeds it, and then lets users ask questions in plain language — the system finds the relevant tables, builds a DDL context, and asks an LLM to write the query.

## How it works

```
Your PostgreSQL DB
        │
        ▼
[Step 1] Schema extraction    — tables, columns, DDL, FK relationships, and two-hop
                                 FK paths are stored in a separate metadata DB
        │
        ▼
[Step 2] LLM description      — an LLM writes a one-sentence description for each
                                 table and a short description for each column
        │
        ▼
[Step 3] Embedding            — each table's description + column descriptions are
                                 concatenated into a text blob and embedded with
                                 BAAI/bge-m3 (1024-dim dense vectors, via pgvector)
        │
        ▼
[Step 4] Query API            — user sends a question → question is embedded →
                                 cosine similarity finds the top-K tables →
                                 DDL context is built → LLM writes SQL →
                                 SQL is executed → results returned
```

---

## Prerequisites

- Python 3.11+ (Conda recommended — this project uses the `sql_llm` env)
- **Target DB** — the PostgreSQL database you want to query in natural language. The credentials you provide need at least `SELECT` access on all tables in the `public` schema.
- **Metadata DB** — a separate PostgreSQL instance where this tool stores table descriptions, embeddings, and FK graphs. You set this up once (see below). Requires the `pgvector` extension.
- An OpenAI-compatible LLM endpoint (e.g. vLLM, Ollama) for description generation and SQL generation
- ~2 GB disk for the BGE-M3 embedding model (downloaded automatically on first run)

---

## Setup

### 1. Install dependencies

```bash
conda activate sql_llm
pip install -r requirements.txt
```

### 2. Configure environment

Copy the template and fill in your values:

```bash
cp env/.env.temp env/.env
```

Edit `env/.env`:

```env
# Target database — the one you want to query
DATABASE_IP       = "your-db-host"
DATABASE_PORT     = "5432"
POSTGRES_USER     = "your-db-user"
POSTGRES_DB_NAME  = "your-db-name"
POSTGRES_PASSWORD = "your-db-password"

# Metadata database — stores descriptions and embeddings (you provision this once)
METADATA_DB_HOST     = "your-metadata-db-host"
METADATA_DB_PORT     = "5432"
METADATA_DB_USER     = "nl2sql_user"
METADATA_DB_NAME     = "nl2sql_metadata"
METADATA_DB_PASSWORD = "your-metadata-db-password"

# LLM endpoint — used for description generation and SQL generation
LLM_BASE_URL = "http://your-llm-host:8000/v1"
LLM_MODEL    = "your-model-name"
```

### 3. Set up the metadata database

This is a one-time step. You need a PostgreSQL server where the tool will store schema metadata and embeddings.

Connect to that server as a superuser and create the user and database:

```sql
CREATE USER nl2sql_user WITH PASSWORD 'your-metadata-db-password';
CREATE DATABASE nl2sql_metadata OWNER nl2sql_user;
```

Then apply the migrations in order from the **project root**:

```bash
# Creates tables (table_metadata, table_relationships, registered_databases)
psql -h <metadata-host> -U postgres -d nl2sql_metadata -f migrations/000_create_metadata_db.sql

# Adds the pgvector embedding column and cosine similarity index
psql -h <metadata-host> -U postgres -d nl2sql_metadata -f migrations/001_add_embedding_column.sql

# Adds the two-hop FK path table
psql -h <metadata-host> -U postgres -d nl2sql_metadata -f migrations/002_add_two_hop_paths.sql
```

Migration `000` must run as a superuser (`postgres`) since it creates tables and grants privileges. Migrations `001` and `002` also require superuser for `CREATE EXTENSION` and `ALTER TABLE`.

> **Note:** `pgvector` must be installed on the metadata DB PostgreSQL server before running migration `001`. On Ubuntu: `apt install postgresql-16-pgvector` (adjust version to match yours).

---

## Indexing your database

Run these three steps once for each database you want to make queryable. Re-run them whenever the schema changes.

### Step 1 — Extract schema

Connects to your target database and stores table DDL, column metadata, FK relationships, and two-hop FK paths into the metadata DB.

```bash
conda activate sql_llm
python -m src.run_setup
# or with a custom env file:
python -m src.run_setup env/.env.staging
```

What it stores per table:
- Column names, types, nullable flags, primary keys
- Foreign key relationships (source table/column → target table/column)
- Two-hop paths: tables connected through a shared bridge table (e.g. `orders ──[order_items]── products`)

### Step 2 — Generate descriptions with an LLM

For each table, calls your LLM endpoint to produce:
- A one-sentence description of what the table stores
- A short description for each column (with FK context, e.g. "references customers.id")

These descriptions are what gets embedded, so the better they are, the more accurate the retrieval.

Before running, verify your LLM endpoint is reachable:

```bash
curl http://your-llm-host:8000/v1/models
```

Then generate descriptions:

```bash
cd src
python run_llm_descriptions.py http://your-llm-host:8000/v1
# or with explicit model and DB name:
python run_llm_descriptions.py http://your-llm-host:8000/v1 your-model-name YOUR_DB_NAME
```

`YOUR_DB_NAME` defaults to `POSTGRES_DB_NAME` from `.env` if not passed.

### Step 3 — Embed descriptions

Builds a structured text blob per table (table description + column descriptions + related tables), embeds it with `BAAI/bge-m3`, and writes the 1024-dim vector back to the metadata DB. The BGE-M3 model is downloaded automatically on first run (~2 GB).

```bash
cd src
python run_embedder.py
# or with explicit env and DB name:
python run_embedder.py ../env/.env YOUR_DB_NAME
```

The embedding text blob for a table looks like:

```
Table: orders
Description: Stores customer purchase orders.
Columns:
  - id (integer, PK): unique order identifier
  - customer_id (integer): references customers.id
  - total (numeric): total order amount
Related tables: customers, order_items
```

This is the text the model embeds — and what your question's embedding is compared against at query time.

---

## Running the API

```bash
conda activate sql_llm
cd src
uvicorn api:app --host 0.0.0.0 --port 8080
```

The API loads the BGE-M3 model on startup and keeps it in memory.

### Query endpoint

**POST** `/generate`

```json
{
  "prompt": "Show all orders placed by customers in Baku",
  "top_k": 5
}
```

`top_k` controls how many tables are retrieved by vector search before FK expansion. Default is 5.

**Example with curl:**

```bash
curl -X POST http://localhost:8080/generate \
  -H "Content-Type: application/json" \
  -d '{"prompt": "Show all orders placed by customers in Baku"}'
```

**Response:**

```json
{
  "success": true,
  "processing_time": 1.43,
  "original_prompt": "Show all orders placed by customers in Baku",
  "retrieved_tables": ["orders", "customers"],
  "generated_sql": "SELECT o.* FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.city = 'Baku';",
  "data": [...]
}
```

`retrieved_tables` shows which tables were pulled into the LLM context — useful for debugging retrieval quality.

---

## Retrieval pipeline (what happens per request)

1. The user's question is embedded with BGE-M3 (same model used at index time)
2. Cosine similarity search against `table_metadata.embedding` returns the top-K most relevant tables
3. FK expansion: directly FK-connected tables are added to the context
4. Two-hop bridge expansion: tables connected through a shared FK bridge are added
5. DDL context is built: `CREATE TABLE` statements with column descriptions for every retrieved table
6. The DDL context + user question is sent to the LLM, which writes a SQL query
7. The query is executed against the target DB and results are returned

---

## Re-indexing

If you add tables, change column names, or want to refresh descriptions, re-run all three steps from the project root:

```bash
conda activate sql_llm
python -m src.run_setup
cd src
python run_llm_descriptions.py http://your-llm-host:8000/v1
python run_embedder.py
```

---

## Running tests

```bash
conda activate sql_llm
python -m pytest tests/ -v
```
