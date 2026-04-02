"""
Unit tests for store_two_hop_paths() in metadata_store.py

Uses a mock psycopg2 connection — no real database required.
"""
from unittest.mock import MagicMock, call, patch
import pytest

from metadata_store import store_two_hop_paths
from schema_extractor.path_builder import TwoHopPath


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_mock_conn():
    """Return a mock psycopg2 connection with a usable cursor context manager."""
    mock_cursor = MagicMock()
    mock_conn   = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__  = MagicMock(return_value=False)
    return mock_conn, mock_cursor


def make_path(table_a: str, bridge: str, table_b: str) -> TwoHopPath:
    return TwoHopPath(table_a=table_a, bridge_table=bridge, table_b=table_b)


# ── store_two_hop_paths ───────────────────────────────────────────────────────

class TestStoreTwoHopPaths:
    def test_deletes_existing_rows_before_insert(self):
        conn, cur = make_mock_conn()
        store_two_hop_paths(conn, db_id=1, paths=[])
        # First execute call must be the DELETE
        first_call_sql = cur.execute.call_args_list[0][0][0]
        assert "DELETE" in first_call_sql.upper()
        assert "two_hop_paths" in first_call_sql

    def test_delete_uses_correct_db_id(self):
        conn, cur = make_mock_conn()
        store_two_hop_paths(conn, db_id=42, paths=[])
        first_call_args = cur.execute.call_args_list[0][0]
        assert first_call_args[1] == {"db_id": 42}

    def test_calls_executemany_with_correct_rows(self):
        conn, cur = make_mock_conn()
        paths = [
            make_path("employees", "contracts", "orders"),
            make_path("customers", "invoices",  "products"),
        ]
        store_two_hop_paths(conn, db_id=1, paths=paths)

        cur.executemany.assert_called_once()
        _, rows = cur.executemany.call_args[0]

        assert len(rows) == 2
        assert rows[0] == {"db_id": 1, "table_a": "employees", "bridge_table": "contracts", "table_b": "orders"}
        assert rows[1] == {"db_id": 1, "table_a": "customers", "bridge_table": "invoices",  "table_b": "products"}

    def test_empty_paths_only_deletes(self):
        conn, cur = make_mock_conn()
        store_two_hop_paths(conn, db_id=1, paths=[])
        cur.executemany.assert_called_once()
        _, rows = cur.executemany.call_args[0]
        assert rows == []

    def test_commits_after_insert(self):
        conn, cur = make_mock_conn()
        store_two_hop_paths(conn, db_id=1, paths=[make_path("a", "b", "c")])
        conn.commit.assert_called_once()

    def test_db_id_propagated_to_all_rows(self):
        conn, cur = make_mock_conn()
        paths = [make_path("a", "b", "c"), make_path("x", "y", "z")]
        store_two_hop_paths(conn, db_id=99, paths=paths)
        _, rows = cur.executemany.call_args[0]
        assert all(r["db_id"] == 99 for r in rows)
