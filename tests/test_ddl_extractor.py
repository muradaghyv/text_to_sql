"""
Unit tests for ddl_extractor.py

Tests build_ddl(), ColummnInfo, and the pg_description comment extractors.
The comment-extractor tests mock psycopg2 — no real DB connection needed.
"""
from unittest.mock import MagicMock

import pytest
from schema_extractor.ddl_extractor import (
    ColummnInfo,
    build_ddl,
    get_column_comments,
    get_table_comment,
)


def _mock_conn(rows):
    """Return a mock psycopg2 connection whose cursor.fetchall/fetchone returns `rows`."""
    cursor = MagicMock()
    cursor.fetchall.return_value = rows
    cursor.fetchone.return_value = rows[0] if rows else None
    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cursor


class TestColumnInfoDescription:
    def test_default_description_is_empty(self):
        col = ColummnInfo(
            name="id",
            data_type="integer",
            is_nullable=False,
            column_default=None,
        )
        assert col.description == ""

    def test_description_can_be_set(self):
        col = ColummnInfo(
            name="email",
            data_type="text",
            is_nullable=False,
            column_default=None,
            description="user's primary email",
        )
        assert col.description == "user's primary email"


class TestGetColumnComments:
    def test_returns_only_columns_with_non_empty_comments(self):
        conn, _ = _mock_conn([
            {"column_name": "id",     "comment": "primary key"},
            {"column_name": "email",  "comment": "contact email"},
            {"column_name": "notes",  "comment": None},
            {"column_name": "blank",  "comment": ""},
        ])
        result = get_column_comments(conn, table_name="users")
        assert result == {"id": "primary key", "email": "contact email"}

    def test_returns_empty_dict_when_no_rows(self):
        conn, _ = _mock_conn([])
        assert get_column_comments(conn, table_name="empty") == {}


class TestGetTableComment:
    def test_returns_comment_string_when_present(self):
        conn, _ = _mock_conn([{"comment": "stores user accounts"}])
        assert get_table_comment(conn, table_name="users") == "stores user accounts"

    def test_returns_empty_when_comment_is_null(self):
        conn, _ = _mock_conn([{"comment": None}])
        assert get_table_comment(conn, table_name="users") == ""

    def test_returns_empty_when_no_row(self):
        conn, _ = _mock_conn([])
        assert get_table_comment(conn, table_name="missing") == ""


class TestBuildDDL:
    def _make_col(self, name, dtype, nullable=True, default=None, pk=False, unique=False):
        return ColummnInfo(
            name=name,
            data_type=dtype,
            is_nullable=nullable,
            column_default=default,
            is_primary_key=pk,
            is_unique=unique,
        )

    def test_basic_table(self):
        cols = [
            self._make_col("id", "integer", nullable=False, pk=True),
            self._make_col("name", "character varying", nullable=False),
        ]
        ddl = build_ddl("users", cols)
        assert ddl.startswith("CREATE TABLE 'public'.users (")
        assert "id integer NOT NULL PRIMARY KEY" in ddl
        assert "name character varying NOT NULL" in ddl
        assert ddl.endswith(");")

    def test_nullable_column_has_no_not_null(self):
        cols = [self._make_col("notes", "text", nullable=True)]
        ddl = build_ddl("t", cols)
        assert "NOT NULL" not in ddl

    def test_default_value_is_included(self):
        cols = [self._make_col("created_at", "timestamp", nullable=False, default="now()")]
        ddl = build_ddl("t", cols)
        assert "DEFAULT now()" in ddl

    def test_unique_column(self):
        cols = [self._make_col("email", "text", nullable=False, unique=True)]
        ddl = build_ddl("t", cols)
        assert "UNIQUE" in ddl

    def test_pk_takes_priority_over_unique(self):
        """A column that is both PK and UNIQUE should only show PRIMARY KEY."""
        cols = [self._make_col("id", "integer", nullable=False, pk=True, unique=True)]
        ddl = build_ddl("t", cols)
        assert "PRIMARY KEY" in ddl
        assert "UNIQUE" not in ddl

    def test_multiple_columns_separated_by_commas(self):
        cols = [
            self._make_col("a", "integer"),
            self._make_col("b", "text"),
            self._make_col("c", "boolean"),
        ]
        ddl = build_ddl("t", cols)
        # Three columns → two commas between them
        assert ddl.count(",") == 2

    def test_empty_table(self):
        ddl = build_ddl("empty_table", [])
        # build_ddl always wraps with \n...\n so an empty column list yields a blank line
        assert "CREATE TABLE 'public'.empty_table (\n\n);" == ddl
