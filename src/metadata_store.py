"""
Handles writing extracted schema metadata into the nl2sql_metadata database.

Public surface:
    get_metadata_connection — open a psycopg2 connection to the metadata DB
    register_database       — upsert a row in registered_databases (legacy)
    get_db_id_by_name       — look up an admin-inserted row's id by db_name
    store_table_metadata    — upsert one row in table_metadata
    store_fk_relationships  — bulk-upsert rows in table_relationships
    store_two_hop_paths     — replace two_hop_paths rows for a db_id
    list_unindexed_dbs      — registered_databases rows that need indexing,
                              with passwords decrypted via pgcrypto
    clear_db_indexed_data   — wipe child rows for a db_id (re-index cleanup)
    set_indexing_error      — mark a registered_databases row as failed
    clear_indexing_error    — mark a registered_databases row as healthy

Connection is made to the METADATA database (nl2sql_metadata), which is
separate from the target database being indexed.

Expected .env keys:
    METADATA_DB_HOST
    METADATA_DB_PORT          (default 5432)
    METADATA_DB_USER
    METADATA_DB_NAME
    METADATA_DB_PASSWORD
    DB_CRED_ENCRYPTION_KEY    (only needed for list_unindexed_dbs)
"""
import json
from dataclasses import asdict

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import os

from schema_extractor.ddl_extractor import TableDDL
from schema_extractor.fk_extractor import FKRelationship
from schema_extractor.path_builder import TwoHopPath

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DEFAULT_ENV = os.path.join(_PROJECT_ROOT, "env", ".env")


def get_metadata_connection(env_path: str = _DEFAULT_ENV) -> psycopg2.extensions.connection:
    """Open a connection to the nl2sql_metadata database."""
    load_dotenv(env_path)

    credentials = {
        "host":     os.getenv("METADATA_DB_HOST"),
        "port":     os.getenv("METADATA_DB_PORT", "5432"),
        "database": os.getenv("METADATA_DB_NAME"),
        "user":     os.getenv("METADATA_DB_USER"),
        "password": os.getenv("METADATA_DB_PASSWORD"),
    }

    missing = [k for k, v in credentials.items() if not v]
    if missing:
        raise ValueError(f"Missing metadata DB credentials in .env: {missing}")

    return psycopg2.connect(**credentials, cursor_factory=psycopg2.extras.RealDictCursor)


def register_database(
    conn: psycopg2.extensions.connection,
    db_name: str,
    host: str,
    port: int = 5432,
    schema_name: str = "public",
    description: str = None,
) -> int:
    """
    Insert or update a row in registered_databases.

    On conflict (same db_name) the host/port/schema/description and
    updated_at timestamp are refreshed.

    Returns the auto-assigned id (db_id) used as FK in other tables.
    """
    query = """
        INSERT INTO registered_databases
            (db_name, host, port, schema_name, description, indexed_at, updated_at)
        VALUES
            (%(db_name)s, %(host)s, %(port)s, %(schema_name)s, %(description)s, now(), now())
        ON CONFLICT (db_name) DO UPDATE SET
            host        = EXCLUDED.host,
            port        = EXCLUDED.port,
            schema_name = EXCLUDED.schema_name,
            description = EXCLUDED.description,
            updated_at  = now()
        RETURNING id;
    """
    with conn.cursor() as cur:
        cur.execute(query, {
            "db_name":     db_name,
            "host":        host,
            "port":        port,
            "schema_name": schema_name,
            "description": description,
        })
        row = cur.fetchone()
    conn.commit()
    return row["id"]


def store_table_metadata(
    conn: psycopg2.extensions.connection,
    db_id: int,
    table_ddl: TableDDL,
) -> None:
    """
    Upsert one row in table_metadata.

    columns_info is stored as a JSONB array where each element is the dict
    representation of a ColummnInfo dataclass (including its `description`
    field, populated from pg_description when set on the target DB).
    table_description is the table-level pg_description comment ("" if none).

    On re-index, the rule for both `table_description` and per-column
    `description` is: a non-empty value from the target DB wins; an empty
    value preserves whatever was stored before (so a later LLM-description
    run is not wiped by a re-extraction).

    Other fields (data_type, nullability, defaults, PK/UQ flags, ddl_text)
    are always refreshed from the target DB — they are the authoritative
    source.
    """
    fetch_existing = """
        SELECT table_description, columns_info
        FROM table_metadata
        WHERE db_id = %(db_id)s
          AND schema_name = %(schema_name)s
          AND table_name = %(table_name)s;
    """
    with conn.cursor() as cur:
        cur.execute(fetch_existing, {
            "db_id":       db_id,
            "schema_name": table_ddl.schema_name,
            "table_name":  table_ddl.table_name,
        })
        existing = cur.fetchone()

    existing_col_desc: dict[str, str] = {}
    existing_table_desc: str = ""
    if existing:
        existing_table_desc = existing.get('table_description') or ""
        for col in (existing.get('columns_info') or []):
            if col.get('description'):
                existing_col_desc[col['name']] = col['description']

    merged_columns = []
    for col in table_ddl.columns:
        col_dict = asdict(col)
        if not col_dict.get('description'):
            col_dict['description'] = existing_col_desc.get(col_dict['name'], "")
        merged_columns.append(col_dict)

    table_description = table_ddl.table_description or existing_table_desc

    upsert = """
        INSERT INTO table_metadata
            (db_id, schema_name, table_name, table_description,
             columns_info, ddl_text, created_at, updated_at)
        VALUES
            (%(db_id)s, %(schema_name)s, %(table_name)s, %(table_description)s,
             %(columns_info)s::jsonb, %(ddl_text)s, now(), now())
        ON CONFLICT (db_id, schema_name, table_name) DO UPDATE SET
            table_description = EXCLUDED.table_description,
            columns_info      = EXCLUDED.columns_info,
            ddl_text          = EXCLUDED.ddl_text,
            updated_at        = now();
    """
    with conn.cursor() as cur:
        cur.execute(upsert, {
            "db_id":             db_id,
            "schema_name":       table_ddl.schema_name,
            "table_name":        table_ddl.table_name,
            "table_description": table_description,
            "columns_info":      json.dumps(merged_columns),
            "ddl_text":          table_ddl.ddl_text,
        })
    conn.commit()


def store_fk_relationships(
    conn: psycopg2.extensions.connection,
    db_id: int,
    fks: list[FKRelationship],
) -> None:
    """
    Bulk-upsert FK relationships into table_relationships.

    Uses ON CONFLICT DO NOTHING so re-running the indexer is safe
    (existing rows are left untouched).
    """
    query = """
        INSERT INTO table_relationships (
            db_id,
            source_schema, source_table, source_column,
            target_schema, target_table, target_column,
            relationship_type
        )
        VALUES (
            %(db_id)s,
            %(source_schema)s, %(source_table)s, %(source_column)s,
            %(target_schema)s, %(target_table)s, %(target_column)s,
            %(relationship_type)s
        )
        ON CONFLICT (
            db_id,
            source_schema, source_table, source_column,
            target_schema, target_table, target_column
        ) DO NOTHING;
    """
    with conn.cursor() as cur:
        for fk in fks:
            cur.execute(query, {
                "db_id":             db_id,
                "source_schema":     fk.source_schema,
                "source_table":      fk.source_table,
                "source_column":     fk.source_column,
                "target_schema":     fk.target_schema,
                "target_table":      fk.target_table,
                "target_column":     fk.target_column,
                "relationship_type": fk.relationship_type,
            })
    conn.commit()


def store_two_hop_paths(
    conn: psycopg2.extensions.connection,
    db_id: int,
    paths: list[TwoHopPath],
) -> None:
    """
    Bulk-insert two-hop paths into two_hop_paths.
    Clears existing rows for this db_id before inserting so re-running is safe.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM two_hop_paths WHERE db_id = %(db_id)s", {"db_id": db_id})
        cur.executemany(
            """
            INSERT INTO two_hop_paths (db_id, table_a, bridge_table, table_b)
            VALUES (%(db_id)s, %(table_a)s, %(bridge_table)s, %(table_b)s)
            """,
            [
                {
                    "db_id":        db_id,
                    "table_a":      p.table_a,
                    "bridge_table": p.bridge_table,
                    "table_b":      p.table_b,
                }
                for p in paths
            ],
        )
    conn.commit()


def get_db_id_by_name(
    conn: psycopg2.extensions.connection,
    db_name: str,
) -> int | None:
    """Return the registered_databases.id for db_name, or None if not registered."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM registered_databases WHERE db_name = %(db_name)s",
            {"db_name": db_name},
        )
        row = cur.fetchone()
    return row["id"] if row else None


def list_unindexed_dbs(
    conn: psycopg2.extensions.connection,
    encryption_key: str,
) -> list[dict]:
    """
    Return registered_databases rows that need (re-)indexing, with the password
    decrypted from db_password_encrypted using pgcrypto.

    A row is considered unindexed when EITHER no rows exist in table_metadata
    for its id (never indexed), OR indexing_error is set (last attempt failed).
    The caller indexes the row, then on success calls clear_indexing_error;
    on failure calls set_indexing_error.

    Each returned dict contains: id, db_name, host, port, schema_name, db_user,
    db_password, indexing_error.
    """
    query = """
        SELECT
            rd.id,
            rd.db_name,
            rd.host,
            rd.port,
            rd.schema_name,
            rd.db_user,
            CASE
                WHEN rd.db_password_encrypted IS NULL THEN NULL
                ELSE pgp_sym_decrypt(rd.db_password_encrypted, %(key)s)
            END AS db_password,
            rd.indexing_error
        FROM registered_databases rd
        LEFT JOIN (
            SELECT db_id, COUNT(*) AS table_count
            FROM table_metadata
            GROUP BY db_id
        ) tm ON tm.db_id = rd.id
        WHERE COALESCE(tm.table_count, 0) = 0
           OR rd.indexing_error IS NOT NULL
        ORDER BY rd.id;
    """
    with conn.cursor() as cur:
        cur.execute(query, {"key": encryption_key})
        return [dict(r) for r in cur.fetchall()]


def list_dbs_needing_privilege_sync(
    conn: psycopg2.extensions.connection,
    encryption_key: str,
) -> list[dict]:
    """
    Return registered_databases rows that have use_privilege_sync=true AND no
    rows yet in emp_table_access for that db_id, with the password decrypted.

    Used by the orchestrator after indexing to populate access control on
    flag-enabled DBs. The flag is set per-row by whoever inserts the row
    (admin panel or manual psql). To force a re-sync after target-DB role
    changes, delete the rows in emp_table_access for that db_id and restart.

    Same return shape as list_unindexed_dbs.
    """
    query = """
        SELECT
            rd.id,
            rd.db_name,
            rd.host,
            rd.port,
            rd.schema_name,
            rd.db_user,
            CASE
                WHEN rd.db_password_encrypted IS NULL THEN NULL
                ELSE pgp_sym_decrypt(rd.db_password_encrypted, %(key)s)
            END AS db_password,
            rd.indexing_error
        FROM registered_databases rd
        LEFT JOIN (
            SELECT db_id, COUNT(*) AS access_rows
            FROM emp_table_access
            GROUP BY db_id
        ) eta ON eta.db_id = rd.id
        WHERE rd.use_privilege_sync = TRUE
          AND COALESCE(eta.access_rows, 0) = 0
          AND rd.indexing_error IS NULL
        ORDER BY rd.id;
    """
    with conn.cursor() as cur:
        cur.execute(query, {"key": encryption_key})
        return [dict(r) for r in cur.fetchall()]


def clear_db_indexed_data(
    conn: psycopg2.extensions.connection,
    db_id: int,
) -> None:
    """
    Delete all child rows for a db_id (table_metadata, table_relationships,
    two_hop_paths). Used before retrying a failed indexing run so the next
    attempt starts from a clean state.

    The registered_databases row itself is left intact.
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM table_metadata     WHERE db_id = %(id)s", {"id": db_id})
        cur.execute("DELETE FROM table_relationships WHERE db_id = %(id)s", {"id": db_id})
        cur.execute("DELETE FROM two_hop_paths      WHERE db_id = %(id)s", {"id": db_id})
    conn.commit()


def set_indexing_error(
    conn: psycopg2.extensions.connection,
    db_id: int,
    error: str,
) -> None:
    """Record an error message on a registered_databases row."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE registered_databases
            SET indexing_error = %(error)s, updated_at = now()
            WHERE id = %(id)s
            """,
            {"id": db_id, "error": error},
        )
    conn.commit()


def clear_indexing_error(
    conn: psycopg2.extensions.connection,
    db_id: int,
) -> None:
    """Clear indexing_error after a successful indexing run."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE registered_databases
            SET indexing_error = NULL, updated_at = now()
            WHERE id = %(id)s
            """,
            {"id": db_id},
        )
    conn.commit()
