**Query for extracting foreign key connections**
```sql
SELECT
    kcu.table_schema AS source_schema,
    kcu.table_name AS source_table,
    kcu.column_name AS source_column,
    ccu.table_schema AS target_schema,
    ccu.table_name AS target_table,
    ccu.column_name AS target_column,
    tc.constraint_type
FROM
    information_schema.table_constraints AS tc
    JOIN information_schema.key_column_usage AS kcu
      ON tc.constraint_name = kcu.constraint_name
      AND tc.table_schema = kcu.table_schema
    JOIN information_schema.constraint_column_usage AS ccu
      ON ccu.constraint_name = tc.constraint_name
      AND ccu.table_schema = tc.table_schema
WHERE tc.constraint_type = 'FOREIGN KEY';
```

# Roadmap for RAG Implementation
* The following schema is approximately shows how process will be performed: 
┌─────────────────────────────────────────────────────────┐
│                    SETUP  (one-time)                     │
│                                                          │
│  Target DB ──→ Extract Schema ──→ Generate Descriptions  │
│                                         ↓                │
│                              Embed Descriptions          │
│                                         ↓                │
│                              Store in Metadata DB        │
└─────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│                   RUNTIME  (per query)                   │
│                                                          │
│  User Prompt ──→ Embed Prompt ──→ Vector Search          │
│                                         ↓                │
│                              FK Expansion + Filter       │
│                                         ↓                │
│                              Build DDL Context           │
│                                         ↓                │
│                              LLM → SQL Query             │
│                                         ↓                │
│                              Execute on Target DB        │
│                                         ↓                │
│                              Return Results              │
└─────────────────────────────────────────────────────────┘

# Areas for Improvement

## Performance
1. **Async embedding** — BGE-M3 runs synchronously on CPU, blocking the FastAPI event loop.
   Wrap the embedder call in `asyncio.run_in_executor` so concurrent requests don't queue behind each other.

2. **Embedding cache** — identical questions re-embed unnecessarily.
   Add an LRU cache (e.g. `functools.lru_cache`) on query vectors to skip re-embedding for repeated prompts.

## Robustness
3. **LLM retry logic** — if the generated SQL fails to execute on ERPHUB, retry once by feeding the error
   message back to the LLM ("this query failed with error X, fix it"). Makes the system self-correcting.

4. **Query validation** — validate LLM-generated SQL against the retrieved table/column names before
   executing, using `sqlglot`. Catches hallucinated table or column names early.

## Codebase
5. **Config dataclass** — `top_k`, model name, LLM URL, and other tunables are scattered across files.
   Centralise them in a `src/config.py` for easier tuning.

6. **Retrieval tuning** — test with more varied prompts and inspect `retrieved_tables` in responses.
   If wrong tables are retrieved, adjust `top_k` or consider adding a reranker (e.g. BGE reranker)
   after the initial vector search.