-- Create metadata database schema
-- Run this against nl2sql_metadata before running any other migration.
--
-- psql -h <host> -U postgres -d nl2sql_metadata -f migrations/000_create_metadata_db.sql

-- Databases that have been indexed
CREATE TABLE IF NOT EXISTS registered_databases (
    id          SERIAL PRIMARY KEY,
    db_name     VARCHAR(255) NOT NULL UNIQUE,
    host        VARCHAR(255) NOT NULL,
    port        INTEGER      NOT NULL DEFAULT 5432,
    schema_name VARCHAR(255) NOT NULL DEFAULT 'public',
    description TEXT,
    indexed_at  TIMESTAMP    NOT NULL DEFAULT now(),
    updated_at  TIMESTAMP    NOT NULL DEFAULT now()
);

-- Metadata about each table
CREATE TABLE IF NOT EXISTS table_metadata (
    id                SERIAL PRIMARY KEY,
    db_id             INTEGER      NOT NULL REFERENCES registered_databases(id) ON DELETE CASCADE,
    schema_name       VARCHAR(255) NOT NULL DEFAULT 'public',
    table_name        VARCHAR(255) NOT NULL,
    table_description TEXT,
    columns_info      JSONB        NOT NULL,
    ddl_text          TEXT         NOT NULL,
    -- embedding vector(1024) — added by migration 001 after pgvector is installed
    created_at        TIMESTAMP    NOT NULL DEFAULT now(),
    updated_at        TIMESTAMP    NOT NULL DEFAULT now(),

    UNIQUE (db_id, schema_name, table_name)
);

-- FK relationships extracted from information_schema
CREATE TABLE IF NOT EXISTS table_relationships (
    id                SERIAL PRIMARY KEY,
    db_id             INTEGER      NOT NULL REFERENCES registered_databases(id) ON DELETE CASCADE,
    source_schema     VARCHAR(255) NOT NULL,
    source_table      VARCHAR(255) NOT NULL,
    source_column     VARCHAR(255) NOT NULL,
    target_schema     VARCHAR(255) NOT NULL,
    target_table      VARCHAR(255) NOT NULL,
    target_column     VARCHAR(255) NOT NULL,
    relationship_type VARCHAR(50)  NOT NULL DEFAULT 'FOREIGN KEY',

    UNIQUE (db_id, source_schema, source_table, source_column, target_schema, target_table, target_column)
);

-- Index for fast lookup by db
CREATE INDEX IF NOT EXISTS idx_table_metadata_db_id ON table_metadata (db_id);

-- Index for fast lookup by source/target table
CREATE INDEX IF NOT EXISTS idx_relationships_source ON table_relationships (db_id, source_schema, source_table);
CREATE INDEX IF NOT EXISTS idx_relationships_target ON table_relationships (db_id, target_schema, target_table);

-- Grant nl2sql_user access to everything in the metadata DB
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO nl2sql_user;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO nl2sql_user;

-- Grant nl2sql_user read-only access in the target DB
GRANT CONNECT ON DATABASE postgres TO nl2sql_user;
GRANT USAGE ON SCHEMA public TO nl2sql_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO nl2sql_user;
