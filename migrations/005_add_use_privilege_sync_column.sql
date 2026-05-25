-- Migration 005: per-DB toggle for the privilege-sync step
--
-- The privilege-sync pipeline is ERPHUB-specific — it expects the target DB
-- to expose `privileges`, `emp_roles`, and `role_privileges` tables in the
-- public schema. Most target DBs won't have those tables, so we keep the
-- step opt-in via a column on registered_databases.
--
-- When the admin inserts an ERPHUB-style row they set use_privilege_sync=true.
-- The startup orchestrator runs the sync for any registered DB that has
-- use_privilege_sync=true AND empty emp_table_access for that db_id (so
-- re-runs are cheap). To force a re-sync after target-DB role changes,
-- delete the rows in emp_table_access for that db_id and restart.

ALTER TABLE registered_databases
    ADD COLUMN IF NOT EXISTS use_privilege_sync BOOLEAN NOT NULL DEFAULT FALSE;
