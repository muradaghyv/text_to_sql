"""
Handles writing extracted schema metadata into the nl2sql_metadata database.

Provides three main operations:
    register_database   — upsert a row in registered_databases, return db_id
    store_table_metadata — upsert a row in table_metadata
    store_fk_relationships — bulk-upsert rows in table_relationships

Connection is made to the METADATA database (nl2sql_metadata), which is
separate from the target database being indexed.

Expected .env keys (in addition to target-DB keys already present):
    METADATA_DB_HOST
    METADATA_DB_PORT       (default 5432)
    METADATA_DB_USER
    METADATA_DB_NAME
    METADATA_DB_PASSWORD
"""
import json
from dataclasses import asdict

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv
import os

from schema_extractor.ddl_extractor import TableDDL
from schema_extractor.fk_extractor import FKRelationship

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

    columns_info is stored as a JSONB array where each element is the
    dict representation of a ColummnInfo dataclass.
    On conflict (same db_id + schema_name + table_name) the columns and
    DDL are refreshed.
    """
    columns_info_json = json.dumps([asdict(col) for col in table_ddl.columns])

    query = """
        INSERT INTO table_metadata
            (db_id, schema_name, table_name, columns_info, ddl_text, created_at, updated_at)
        VALUES
            (%(db_id)s, %(schema_name)s, %(table_name)s,
             %(columns_info)s::jsonb, %(ddl_text)s, now(), now())
        ON CONFLICT (db_id, schema_name, table_name) DO UPDATE SET
            columns_info = EXCLUDED.columns_info,
            ddl_text     = EXCLUDED.ddl_text,
            updated_at   = now();
    """
    with conn.cursor() as cur:
        cur.execute(query, {
            "db_id":        db_id,
            "schema_name":  table_ddl.schema_name,
            "table_name":   table_ddl.table_name,
            "columns_info": columns_info_json,
            "ddl_text":     table_ddl.ddl_text,
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
