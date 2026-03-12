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