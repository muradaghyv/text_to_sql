# text-to-SQL

A RAG-based text-to-SQL system for PostgreSQL. You describe your database once, embed it, and then ask questions in plain language — the system finds the relevant tables, builds a DDL context, and asks an LLM to write the query.

## How it works

```
Your PostgreSQL DB
        │
        ▼
[Step 1] Schema extraction    — DDL, columns, FK relationships stored in a metadata DB
        │
        ▼
[Step 2] LLM description      — an LLM writes a one-sentence description for each table
                                 and a short description for each column
        │
        ▼
[Step 3] Embedding            — each table's description + column descriptions are
                                 concatenated into a text blob and embedded with BAAI/bge-m3
                                 (1024-dim dense vectors, stored via pgvector)
        │
        ▼
[Step 4] Query API            — user sends a question → question is embedded → cosine
                                 similarity finds the top-K tables → DDL context is built
                                 → LLM writes SQL → SQL is executed → results returned
```

---

## Prerequisites

- Python 3.11+
- Two PostgreSQL databases:
  - **Target DB** — your existing database that you want to query in natural language
  - **Metadata DB** — a separate PostgreSQL database (`nl2sql_metadata`) where this tool stores table descriptions, embeddings, and FK graphs. Create it before starting.
- pgvector extension installed in the metadata DB
- An OpenAI-compatible LLM endpoint (e.g. vLLM, Ollama) for description generation and query generation
- ~2 GB disk space for the BGE-M3 embedding model (downloaded automatically on first run)

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

Copy the template and fill in your values:

```bash
cp env/.env.temp env/.env
```

Edit `env/.env`:

```env
# Target database (the one you want to query)
DATABASE_IP       = "your-db-host"
DATABASE_PORT     = "5432"
POSTGRES_USER     = "your-db-user"
POSTGRES_DB_NAME  = "your-db-name"
POSTGRES_PASSWORD = "your-db-password"

# Metadata database (stores descriptions and embeddings)
METADATA_DB_HOST     = "your-metadata-db-host"
METADATA_DB_PORT     = "5432"
METADATA_DB_USER     = "nl2sql_user"
METADATA_DB_NAME     = "nl2sql_metadata"
METADATA_DB_PASSWORD = "your-metadata-db-password"

# LLM endpoint (used for description generation and SQL generation)
LLM_BASE_URL = "http://your-llm-host:8000/v1"
LLM_MODEL    = "your-model-name"
```

### 3. Run database migrations

Apply these against your **metadata DB** in order:

```bash
psql -h <metadata-host> -U nl2sql_user -d nl2sql_metadata -f migrations/001_add_embedding_column.sql
psql -h <metadata-host> -U nl2sql_user -d nl2sql_metadata -f migrations/002_add_two_hop_paths.sql
```

---

## Indexing your database

Run these three steps once per database (or re-run when the schema changes).

### Step 1 — Extract schema

Reads your target database and stores table DDL, column metadata, FK relationships, and two-hop FK paths into the metadata DB.

```bash
python -m src.run_setup
# or with a custom env file:
python -m src.run_setup env/.env.staging
```

What it stores per table:
- Column names, types, nullable flags, primary keys
- Foreign key relationships (source table/column → target table/column)
- Two-hop paths: tables that are connected through a bridge table (e.g. `orders ──[order_items]── products`)

### Step 2 — Generate descriptions with an LLM

For each table, calls your LLM endpoint to produce:
- A one-sentence description of what the table stores
- A short description for each column (with FK context, e.g. "references customers.id")

These descriptions are what gets embedded, so the better they are, the more accurate the retrieval.

```bash
cd src
python run_llm_descriptions.py http://your-llm-host:8000/v1
# or with explicit model and DB name:
python run_llm_descriptions.py http://your-llm-host:8000/v1 your-model-name YOUR_DB_NAME
```

### Step 3 — Embed descriptions

Builds a structured text blob per table (table description + column descriptions + related tables), then embeds it with `BAAI/bge-m3` and writes the 1024-dim vector back to the metadata DB.

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

This is the text the model embeds — and the text your question's embedding is compared against at query time.

---

## Running the API

```bash
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

`top_k` controls how many tables are retrieved by vector search before FK expansion. The default is 5.

---

## Retrieval pipeline (what happens per request)

1. The user's question is embedded with BGE-M3 (same model used at index time)
2. Cosine similarity search against `table_metadata.embedding` returns the top-K most relevant tables
3. FK expansion: directly related tables and two-hop bridge tables are added to the context
4. DDL context is built: `CREATE TABLE` statements with column descriptions for every retrieved table
5. The DDL context + user question is sent to the LLM, which writes a SQL query
6. The query is executed against the target DB and results are returned

---

## Re-indexing

If you add tables or change column names, re-run all three steps:

```bash
python -m src.run_setup
cd src && python run_llm_descriptions.py http://your-llm-host:8000/v1
cd src && python run_embedder.py
```
