-- Migration 004: store target-DB credentials inside registered_databases
--
-- The admin panel writes one row per target DB (db_name, host, port, db_user,
-- password). Our setup pipeline indexes any row that does not yet have rows in
-- table_metadata. The API connects to each target DB using these credentials
-- at runtime, replacing the old REGISTERED_DB_CREDENTIALS env var.
--
-- Passwords are stored as BYTEA, encrypted with pgcrypto symmetric encryption.
-- The application encrypts on insert with `pgp_sym_encrypt(plain, key)` and
-- decrypts on read with `pgp_sym_decrypt(ciphertext, key)`. The key lives in
-- the DB_CRED_ENCRYPTION_KEY env var; it is never written to the database.
--
-- Run as the metadata-DB superuser:
--   psql -h <host> -U postgres -d nl2sql_metadata \
--        -f migrations/004_add_target_db_credentials.sql

-- pgcrypto provides pgp_sym_encrypt / pgp_sym_decrypt.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE registered_databases
    ADD COLUMN IF NOT EXISTS db_user                VARCHAR(255),
    ADD COLUMN IF NOT EXISTS db_password_encrypted  BYTEA,
    ADD COLUMN IF NOT EXISTS indexing_error         TEXT;
