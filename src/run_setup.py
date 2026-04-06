"""
Phase 2 — Schema Extraction Orchestrator

Reads all tables from a target PostgreSQL database, extracts DDL and FK
relationships, and stores the results in the nl2sql_metadata database.

Run from the project root:
    python -m src.run_setup
    python -m src.run_setup env/.env.staging   # pass a custom env path

What it does:
    1. Connect to target DB (credentials from .env)
    2. List all tables in the public schema
    3. Extract DDL + column metadata for every table
    4. Extract all FK relationships
    5. Build the FK adjacency graph and compute two-hop paths (logged only)
    6. Register the target DB in nl2sql_metadata
    7. Store table metadata rows
    8. Store FK relationship rows
"""
import sys
import os

from dotenv import load_dotenv

# Project root is one level above this file (src/../)
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_ENV_PATH = os.path.join(PROJECT_ROOT, "env", ".env")

from logger import get_logger
from schema_extractor.list_tables import get_connection, list_table_names
from schema_extractor.ddl_extractor import extract_table_ddl
from schema_extractor.fk_extractor import extract_foreign_keys
from schema_extractor.path_builder import build_adjacency, find_two_hop_paths
from metadata_store import (
    get_metadata_connection,
    register_database,
    store_table_metadata,
    store_fk_relationships,
    store_two_hop_paths,
)


def run(env_path: str = DEFAULT_ENV_PATH) -> None:
    logger = get_logger(__name__)
    load_dotenv(env_path)

    target_db_name = os.getenv("POSTGRES_DB_NAME")
    target_db_host = os.getenv("DATABASE_IP")
    target_db_port = int(os.getenv("DATABASE_PORT", "5432"))

    # ── Step 1: connect to target DB ────────────────────────────────────────
    logger.info("[1/6] Connecting to target DB: '%s' @ %s:%s", target_db_name, target_db_host, target_db_port)
    try:
        target_conn = get_connection(env_path=env_path)
    except Exception as e:
        logger.error("Failed to connect to target DB: %s", e, exc_info=True)
        raise
    logger.info("      Connected.")

    # ── Step 2: list tables ──────────────────────────────────────────────────
    logger.info("[2/6] Listing tables in public schema...")
    tables = list_table_names(target_conn)
    preview = ", ".join(tables[:5]) + ("..." if len(tables) > 5 else "")
    logger.info("      Found %d table(s): %s", len(tables), preview)

    # ── Step 3: extract FK relationships ────────────────────────────────────
    logger.info("[3/6] Extracting FK relationships...")
    fks = extract_foreign_keys(target_conn)
    logger.info("      Found %d FK relationship(s).", len(fks))

    # ── Step 4: compute two-hop paths ───────────────────────────────────────
    logger.info("[4/6] Computing two-hop paths from FK graph...")
    adjacency = build_adjacency(fks)
    two_hop_paths = find_two_hop_paths(adjacency)
    logger.info("      Found %d two-hop path(s).", len(two_hop_paths))
    if two_hop_paths:
        logger.debug("      Sample paths: %s",
                     ", ".join(f"{p.table_a}──[{p.bridge_table}]──{p.table_b}" for p in two_hop_paths[:5]))

    # ── Step 5: connect to metadata DB ──────────────────────────────────────
    logger.info("[5/6] Connecting to metadata DB and registering target DB...")
    try:
        meta_conn = get_metadata_connection(env_path=env_path)
    except Exception as e:
        logger.error("Failed to connect to metadata DB: %s", e, exc_info=True)
        raise
    db_id = register_database(
        conn=meta_conn,
        db_name=target_db_name,
        host=target_db_host,
        port=target_db_port,
        schema_name="public",
    )
    logger.info("      '%s' registered with db_id=%s.", target_db_name, db_id)

    # ── Step 6: store table metadata & FK relationships ──────────────────────
    logger.info("[6/6] Storing metadata for %d table(s)...", len(tables))
    for i, table_name in enumerate(tables, start=1):
        try:
            table_ddl = extract_table_ddl(connection=target_conn, table_name=table_name)
            store_table_metadata(conn=meta_conn, db_id=db_id, table_ddl=table_ddl)
            logger.debug("      [%3d/%d] %s", i, len(tables), table_name)
        except Exception as e:
            logger.error("      Failed on table '%s': %s", table_name, e, exc_info=True)
            raise

    logger.info("      Storing %d FK relationship(s)...", len(fks))
    store_fk_relationships(conn=meta_conn, db_id=db_id, fks=fks)

    logger.info("      Storing %d two-hop path(s)...", len(two_hop_paths))
    store_two_hop_paths(conn=meta_conn, db_id=db_id, paths=two_hop_paths)

    target_conn.close()
    meta_conn.close()

    logger.info(
        "Setup complete — DB: %s | tables: %d | FKs: %d | two-hop paths: %d",
        target_db_name, len(tables), len(fks), len(two_hop_paths),
    )


if __name__ == "__main__":
    env = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ENV_PATH
    run(env_path=env)
