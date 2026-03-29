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

from schema_extractor.list_tables import get_connection, list_table_names
from schema_extractor.ddl_extractor import extract_table_ddl
from schema_extractor.fk_extractor import extract_foreign_keys
from schema_extractor.path_builder import build_adjacency, find_two_hop_paths
from metadata_store import (
    get_metadata_connection,
    register_database,
    store_table_metadata,
    store_fk_relationships,
)


def run(env_path: str = DEFAULT_ENV_PATH) -> None:
    load_dotenv(env_path)

    target_db_name = os.getenv("POSTGRES_DB_NAME")
    target_db_host = os.getenv("DATABASE_IP")
    target_db_port = int(os.getenv("DATABASE_PORT", "5432"))

    # ── Step 1: connect to target DB ────────────────────────────────────────
    print(f"[1/6] Connecting to target DB: '{target_db_name}' @ {target_db_host}:{target_db_port}")
    target_conn = get_connection(env_path=env_path)
    print("      Connected.")

    # ── Step 2: list tables ──────────────────────────────────────────────────
    print("[2/6] Listing tables in public schema...")
    tables = list_table_names(target_conn)
    preview = ", ".join(tables[:5]) + ("..." if len(tables) > 5 else "")
    print(f"      Found {len(tables)} table(s): {preview}")

    # ── Step 3: extract FK relationships ────────────────────────────────────
    print("[3/6] Extracting FK relationships...")
    fks = extract_foreign_keys(target_conn)
    print(f"      Found {len(fks)} FK relationship(s).")

    # ── Step 4: compute two-hop paths (informational) ───────────────────────
    print("[4/6] Computing two-hop paths from FK graph...")
    adjacency = build_adjacency(fks)
    two_hop_paths = find_two_hop_paths(adjacency)
    print(f"      Found {len(two_hop_paths)} two-hop path(s).")
    if two_hop_paths:
        print("      Sample paths:")
        for p in two_hop_paths[:5]:
            print(f"        {p.table_a}  ──[{p.bridge_table}]──  {p.table_b}")

    # ── Step 5: connect to metadata DB ──────────────────────────────────────
    print("[5/6] Connecting to metadata DB and registering target DB...")
    meta_conn = get_metadata_connection(env_path=env_path)
    db_id = register_database(
        conn=meta_conn,
        db_name=target_db_name,
        host=target_db_host,
        port=target_db_port,
        schema_name="public",
    )
    print(f"      '{target_db_name}' registered with db_id={db_id}.")

    # ── Step 6: store table metadata & FK relationships ──────────────────────
    print(f"[6/6] Storing metadata for {len(tables)} table(s)...")
    for i, table_name in enumerate(tables, start=1):
        table_ddl = extract_table_ddl(connection=target_conn, table_name=table_name)
        store_table_metadata(conn=meta_conn, db_id=db_id, table_ddl=table_ddl)
        print(f"      [{i:>3}/{len(tables)}] {table_name}")

    print(f"      Storing {len(fks)} FK relationship(s)...")
    store_fk_relationships(conn=meta_conn, db_id=db_id, fks=fks)

    target_conn.close()
    meta_conn.close()

    print("\n── Setup complete ───────────────────────────────────")
    print(f"  Target database : {target_db_name}")
    print(f"  Tables indexed  : {len(tables)}")
    print(f"  FK relationships: {len(fks)}")
    print(f"  Two-hop paths   : {len(two_hop_paths)}")
    print("─────────────────────────────────────────────────────")


if __name__ == "__main__":
    env = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_ENV_PATH
    run(env_path=env)
