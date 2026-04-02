-- Migration 002: add two_hop_paths table
--
-- Stores pre-computed two-hop join paths between tables that are not
-- directly FK-connected but share a common bridge table.
-- Used at query time to expand the retrieval context with bridge tables.

CREATE TABLE IF NOT EXISTS two_hop_paths (
    id           SERIAL PRIMARY KEY,
    db_id        INTEGER NOT NULL REFERENCES registered_databases(id),
    table_a      VARCHAR(255) NOT NULL,
    bridge_table VARCHAR(255) NOT NULL,
    table_b      VARCHAR(255) NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_two_hop_db_table_a ON two_hop_paths (db_id, table_a);
CREATE INDEX IF NOT EXISTS idx_two_hop_db_table_b ON two_hop_paths (db_id, table_b);
