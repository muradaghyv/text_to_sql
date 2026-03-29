"""
Unit tests for ddl_extractor.py

Tests build_ddl() and the ColummnInfo dataclass — no DB connection needed.
"""
import pytest
from schema_extractor.ddl_extractor import ColummnInfo, build_ddl


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
