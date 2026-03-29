-- Migration 001: add embedding column to table_metadata
-- Run this as the postgres superuser against nl2sql_metadata DB before running run_phase3.py
--
-- psql -h <host> -U postgres -d nl2sql_metadata -f migrations/001_add_embedding_column.sql

-- Step 1: enable pgvector extension
CREATE EXTENSION IF NOT EXISTS vector;

-- Step 2: add the embedding column (1024-dim for BAAI/bge-m3)
ALTER TABLE table_metadata
    ADD COLUMN IF NOT EXISTS embedding vector(1024);

-- Step 3: index for cosine-similarity nearest-neighbor search
-- IVFFlat is efficient for large datasets. lists = sqrt(row_count) is a good rule of thumb.
-- With ~165 tables this index is optional (linear scan is fine), but add it now
-- so it works as the dataset grows.
CREATE INDEX IF NOT EXISTS idx_table_metadata_embedding
    ON table_metadata
    USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 16);
