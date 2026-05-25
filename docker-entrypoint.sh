#!/bin/sh
# Container entrypoint.
#
# Modes (picked from the first argument):
#   api       — default. Ensure the metadata DB and user exist, apply migrations,
#               index any target DBs in registered_databases that are not yet
#               indexed, then run uvicorn.
#               (Step 3 = create DB + migrations + first-run exit message.
#                Step 4 = adds the auto-indexing of unindexed registered_databases rows.)
#
#   describe  — run the optional LLM-description step on existing metadata.
#               (Filled in by step 6.)
#
#   *         — exec the argument as-is (for ad-hoc shell / debugging).

set -e

mode="${1:-api}"
shift || true

# ── helpers ──────────────────────────────────────────────────────────────────

require_env() {
    var_name="$1"
    eval "value=\$$var_name"
    if [ -z "$value" ]; then
        echo "ERROR: $var_name is required" >&2
        exit 1
    fi
}

# Run a psql command as the metadata-DB superuser. First arg = target dbname.
# All remaining args are forwarded to psql.
psql_admin() {
    target_db="$1"
    shift
    PGPASSWORD="$METADATA_DB_ADMIN_PASSWORD" psql \
        --host="$METADATA_DB_HOST" \
        --port="$META_PORT" \
        --username="$METADATA_DB_ADMIN_USER" \
        --dbname="$target_db" \
        --no-psqlrc \
        "$@"
}

# Escape single quotes for safe use inside a SQL string literal (doubles ').
sql_string_escape() {
    printf '%s' "$1" | sed "s/'/''/g"
}

# Ensure the metadata DB and user exist, then apply migrations idempotently.
# Sets META_FRESH=1 if the database was just created (no rows in any table).
init_metadata_db() {
    require_env METADATA_DB_HOST
    require_env METADATA_DB_NAME
    require_env METADATA_DB_USER
    require_env METADATA_DB_PASSWORD
    require_env METADATA_DB_ADMIN_USER
    require_env METADATA_DB_ADMIN_PASSWORD

    META_PORT="${METADATA_DB_PORT:-5432}"

    echo "── Checking metadata DB at ${METADATA_DB_HOST}:${META_PORT} ──"

    # 1. Ensure the application role exists. We do shell-side string assembly
    #    rather than psql -v variables because psql variable substitution is
    #    finicky inside -c commands across versions.
    user_escaped="$(sql_string_escape "$METADATA_DB_USER")"
    pwd_escaped="$(sql_string_escape "$METADATA_DB_PASSWORD")"

    role_exists="$(psql_admin postgres -tAc \
        "SELECT 1 FROM pg_roles WHERE rolname = '$user_escaped'")"
    role_exists="$(echo "$role_exists" | tr -d '[:space:]')"

    if [ -z "$role_exists" ]; then
        echo "  creating role $METADATA_DB_USER"
        psql_admin postgres --quiet --set=ON_ERROR_STOP=1 -c \
            "CREATE ROLE \"$METADATA_DB_USER\" WITH LOGIN PASSWORD '$pwd_escaped';"
    fi

    # 2. Ensure the database exists. CREATE DATABASE cannot run inside a DO
    #    block, so we test-then-create.
    db_exists="$(psql_admin postgres -tAc \
        "SELECT 1 FROM pg_database WHERE datname = '$METADATA_DB_NAME'")"

    META_FRESH=0
    if [ -z "$db_exists" ]; then
        echo "  creating database $METADATA_DB_NAME (owner=$METADATA_DB_USER)"
        psql_admin postgres --quiet --set=ON_ERROR_STOP=1 \
            -c "CREATE DATABASE \"$METADATA_DB_NAME\" OWNER \"$METADATA_DB_USER\";"
        META_FRESH=1
    fi

    # 3. Apply every migration. All migrations are idempotent (IF NOT EXISTS,
    #    ADD COLUMN IF NOT EXISTS, ON CONFLICT, etc.) so we can run them on
    #    every container start.
    echo "── Applying migrations ──"
    for f in /app/migrations/*.sql; do
        echo "  applying $(basename "$f")"
        psql_admin "$METADATA_DB_NAME" \
            --quiet --set=ON_ERROR_STOP=1 --file="$f"
    done
}

# When registered_databases is empty there is nothing to index. Tell the admin
# how to populate it and stop the container.
exit_if_no_registered_dbs() {
    count="$(psql_admin "$METADATA_DB_NAME" -tAc \
        "SELECT COUNT(*) FROM registered_databases")"

    # Trim whitespace from psql output.
    count="$(echo "$count" | tr -d '[:space:]')"

    if [ "$count" = "0" ]; then
        cat <<EOF

────────────────────────────────────────────────────────────────────────
Metadata DB '$METADATA_DB_NAME' is ready, but no target DBs are
registered. Insert one row per target DB into the registered_databases
table and re-run \`docker compose up\`. Example:

  INSERT INTO registered_databases
      (db_name, host, port, schema_name, db_user, db_password_encrypted)
  VALUES
      ('YOUR_DB_NAME', 'your-db-host', 5432, 'public',
       'your-db-user',
       pgp_sym_encrypt('your-db-password',
                       '<value of DB_CRED_ENCRYPTION_KEY>'));

Indexing runs automatically on the next start for every row that does
not yet have rows in table_metadata.
────────────────────────────────────────────────────────────────────────
EOF
        exit 0
    fi
}

# ── modes ────────────────────────────────────────────────────────────────────

case "$mode" in
  api)
    init_metadata_db
    exit_if_no_registered_dbs

    require_env DB_CRED_ENCRYPTION_KEY
    echo "── Indexing any unindexed target DBs ──"
    cd /app/src
    python run_index_unindexed.py

    echo "── Starting API ──"
    exec uvicorn api:app --host 0.0.0.0 --port 8080
    ;;

  describe)
    # Step 6 will fill this in.
    init_metadata_db
    cd /app/src
    exec python run_llm_descriptions.py "$@"
    ;;

  *)
    # Pass-through for ad-hoc commands (e.g. `docker compose run --rm api sh`).
    exec "$mode" "$@"
    ;;
esac
