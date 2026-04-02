"""
Unit tests for query_pipeline/retriever.py

Tests build_ddl_context() — a pure function requiring no database connection.
"""
import json
import pytest
from query_pipeline.retriever import build_ddl_context


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_table(
    table_name: str,
    ddl_text: str,
    table_description: str = None,
    columns_info=None,
) -> dict:
    """Build a table dict as returned by asyncpg (columns_info as JSON string)."""
    return {
        "table_name":        table_name,
        "ddl_text":          ddl_text,
        "table_description": table_description,
        "columns_info":      json.dumps(columns_info) if columns_info is not None else None,
    }


# ── build_ddl_context ─────────────────────────────────────────────────────────

class TestBuildDdlContext:
    def test_single_table_includes_ddl(self):
        tables = [make_table("orders", "CREATE TABLE orders (id integer);")]
        result = build_ddl_context(tables)
        assert "CREATE TABLE orders (id integer);" in result

    def test_table_description_appears_as_comment(self):
        tables = [make_table(
            "orders",
            "CREATE TABLE orders (id integer);",
            table_description="Stores purchase orders",
        )]
        result = build_ddl_context(tables)
        assert "-- Stores purchase orders" in result

    def test_no_description_skips_comment_line(self):
        tables = [make_table("orders", "CREATE TABLE orders (id integer);")]
        result = build_ddl_context(tables)
        assert "-- None" not in result

    def test_column_descriptions_appear(self):
        columns = [
            {"name": "id",    "data_type": "integer", "description": "Primary key"},
            {"name": "email", "data_type": "varchar", "description": "User email address"},
        ]
        tables = [make_table("users", "CREATE TABLE users ();", columns_info=columns)]
        result = build_ddl_context(tables)
        assert "id (integer)" in result
        assert "Primary key" in result
        assert "email (varchar)" in result
        assert "User email address" in result

    def test_column_without_description_still_appears(self):
        columns = [{"name": "id", "data_type": "integer"}]
        tables = [make_table("users", "CREATE TABLE users ();", columns_info=columns)]
        result = build_ddl_context(tables)
        assert "id (integer)" in result

    def test_multiple_tables_separated_by_blank_line(self):
        tables = [
            make_table("orders",   "CREATE TABLE orders ();"),
            make_table("products", "CREATE TABLE products ();"),
        ]
        result = build_ddl_context(tables)
        assert "CREATE TABLE orders ();" in result
        assert "CREATE TABLE products ();" in result
        # two blocks separated by double newline
        assert "\n\n" in result

    def test_empty_table_list_returns_empty_string(self):
        assert build_ddl_context([]) == ""

    def test_columns_info_as_parsed_list(self):
        """columns_info may arrive already parsed (list) instead of a JSON string."""
        columns = [{"name": "id", "data_type": "integer", "description": "PK"}]
        table = {
            "table_name":        "orders",
            "ddl_text":          "CREATE TABLE orders ();",
            "table_description": None,
            "columns_info":      columns,   # already a list, not a string
        }
        result = build_ddl_context([table])
        assert "id (integer)" in result
        assert "PK" in result

    def test_null_columns_info_renders_only_ddl(self):
        tables = [make_table("orders", "CREATE TABLE orders ();", columns_info=None)]
        result = build_ddl_context(tables)
        assert "CREATE TABLE orders ();" in result
        assert "-- Columns:" not in result
