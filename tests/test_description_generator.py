"""
Unit tests for description_generator.py — no DB or embedding model needed.
"""
from description_embedder.description_generator import (
    snake_to_words,
    describe_column,
    generate_table_description,
    build_embedding_text,
    enrich_columns_with_descriptions,
)


def col(name, dtype="integer", nullable=True, pk=False, unique=False):
    return {
        "name": name,
        "data_type": dtype,
        "is_nullable": nullable,
        "column_default": None,
        "is_primary_key": pk,
        "is_unique": unique,
    }


# ── snake_to_words ────────────────────────────────────────────────────────────

class TestSnakeToWords:
    def test_simple_snake_case(self):
        assert snake_to_words("employee_id") == "employee id"

    def test_multi_part(self):
        assert snake_to_words("created_at") == "created at"

    def test_camel_case(self):
        assert snake_to_words("createdAt") == "created at"

    def test_no_underscores(self):
        assert snake_to_words("name") == "name"

    def test_multiple_underscores(self):
        assert snake_to_words("first_last_name") == "first last name"


# ── describe_column ───────────────────────────────────────────────────────────

class TestDescribeColumn:
    def test_primary_key(self):
        desc = describe_column(col("id", pk=True), {})
        assert "primary key" in desc

    def test_fk_takes_priority(self):
        fk_map = {"employee_id": ("employees", "id")}
        desc = describe_column(col("employee_id"), fk_map)
        assert "employees" in desc
        assert "foreign key" in desc

    def test_exact_hint_created_at(self):
        desc = describe_column(col("created_at", dtype="timestamp"), {})
        assert "created" in desc

    def test_exact_hint_email(self):
        desc = describe_column(col("email", dtype="varchar"), {})
        assert "email" in desc

    def test_suffix_hint_amount(self):
        desc = describe_column(col("total_amount"), {})
        assert "amount" in desc

    def test_fallback_humanises_name(self):
        desc = describe_column(col("some_weird_column"), {})
        assert "some weird column" in desc


# ── generate_table_description ────────────────────────────────────────────────

class TestGenerateTableDescription:
    def test_basic(self):
        desc = generate_table_description("employee_contacts")
        assert "employee contacts" in desc

    def test_starts_with_stores(self):
        desc = generate_table_description("invoices")
        assert desc.startswith("Stores")


# ── build_embedding_text ──────────────────────────────────────────────────────

class TestBuildEmbeddingText:
    def _build(self, table="orders", extra_cols=None, fk_map=None, related=None):
        columns = [
            col("id", pk=True),
            col("customer_id"),
            col("total_amount", dtype="numeric"),
        ]
        if extra_cols:
            columns += extra_cols
        return build_embedding_text(
            table_name=table,
            table_description=f"Stores {table} records",
            columns=columns,
            col_fk_map=fk_map if fk_map is not None else {"customer_id": ("customers", "id")},
            related_tables=related if related is not None else ["customers"],
        )

    def test_contains_table_name(self):
        assert "Table: orders" in self._build()

    def test_contains_description(self):
        assert "Stores orders records" in self._build()

    def test_contains_fk_annotation(self):
        text = self._build()
        assert "FK→customers.id" in text

    def test_contains_pk_annotation(self):
        text = self._build()
        assert "[PK]" in text

    def test_contains_related_tables(self):
        text = self._build(related=["customers", "products"])
        assert "Related tables: customers, products" in text

    def test_no_related_tables_line_when_empty(self):
        text = self._build(related=[])
        assert "Related tables" not in text


# ── enrich_columns_with_descriptions ─────────────────────────────────────────

class TestEnrichColumns:
    def test_description_key_added(self):
        columns = [col("id", pk=True), col("name", dtype="varchar")]
        enriched = enrich_columns_with_descriptions(columns, {})
        assert all("description" in c for c in enriched)

    def test_original_keys_preserved(self):
        columns = [col("id", pk=True)]
        enriched = enrich_columns_with_descriptions(columns, {})
        assert enriched[0]["name"] == "id"
        assert enriched[0]["is_primary_key"] is True

    def test_does_not_mutate_original(self):
        columns = [col("id", pk=True)]
        original_len = len(columns[0])
        enrich_columns_with_descriptions(columns, {})
        assert len(columns[0]) == original_len
