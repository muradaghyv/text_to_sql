"""
SQL safety validator for LLM-generated queries.

Rules enforced:
  1. Parses as valid PostgreSQL SQL (via sqlglot).
  2. Exactly one statement — no stacked queries.
  3. Top-level statement must be SELECT.
  4. No DML anywhere in the AST, including inside CTEs
     (WITH x AS (DELETE … RETURNING) SELECT * FROM x).
  5. No access to forbidden system schemas or sensitive catalog tables.
  6. No dangerous built-in functions (file I/O, process control, large objects, dblink…).
  7. All referenced base tables must be in the allowed set that was retrieved
     from the metadata store (catches hallucinated table names).
"""

import sqlglot
import sqlglot.expressions as exp


# ── Deny-lists ────────────────────────────────────────────────────────────────

# PostgreSQL schemas that must never appear in a user-facing query.
FORBIDDEN_SCHEMAS: frozenset[str] = frozenset({
    "pg_catalog",
    "information_schema",
    "pg_toast",
    "pg_temp",
    "pg_toast_temp",
})

# Unqualified table names that live in public / pg_catalog and are sensitive.
FORBIDDEN_TABLES: frozenset[str] = frozenset({
    "pg_shadow",
    "pg_authid",
    "pg_user",
    "pg_roles",
    "pg_auth_members",
    "pg_hba_file_rules",
    "pg_ident_file_mappings",
    "pg_config",
    "pg_file_settings",
})

# Functions that can read files, control processes, transfer data out, or cause DoS.
FORBIDDEN_FUNCTIONS: frozenset[str] = frozenset({
    # File system
    "pg_read_file",
    "pg_read_binary_file",
    "pg_ls_dir",
    "pg_ls_waldir",
    "pg_ls_logdir",
    "pg_ls_archive_statusdir",
    "pg_ls_tmpdir",
    "pg_stat_file",
    # Process control
    "pg_cancel_backend",
    "pg_terminate_backend",
    "pg_reload_conf",
    "pg_rotate_logfile",
    # Large objects
    "lo_import",
    "lo_export",
    "lo_creat",
    "lo_create",
    "lo_unlink",
    "lo_truncate",
    # Remote execution / dblink
    "dblink",
    "dblink_exec",
    "dblink_connect",
    "dblink_connect_u",
    # Sleep / DoS
    "pg_sleep",
    "pg_sleep_for",
    "pg_sleep_until",
    # Admin functions
    "pg_switch_wal",
    "pg_checkpoint",
})

# AST node types that must not appear anywhere in the statement tree.
# This covers DML inside CTEs, subqueries, etc.
FORBIDDEN_NODE_TYPES: tuple = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Drop,
    exp.Create,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Command,      # catches COPY, VACUUM, ANALYZE, REINDEX when sqlglot emits Command
    exp.Set,
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Use,
)


# ── Exception ─────────────────────────────────────────────────────────────────

class SQLValidationError(ValueError):
    """Raised when the generated SQL fails a safety check."""


# ── Validator ─────────────────────────────────────────────────────────────────

def validate_sql(sql: str, allowed_tables: list[str] | None = None) -> None:
    """
    Validate an LLM-generated SQL string against safety rules.

    Args:
        sql:            The SQL string to validate (already stripped of markdown).
        allowed_tables: Table names that appeared in the retrieved schema context.
                        When provided, any table not in this list is rejected as
                        a hallucinated or disallowed reference.

    Raises:
        SQLValidationError: with a human-readable message describing the violation.
    """

    # ── 1. Parse ──────────────────────────────────────────────────────────────
    try:
        statements = sqlglot.parse(sql, dialect="postgres", error_level=sqlglot.ErrorLevel.RAISE)
    except sqlglot.errors.ParseError as exc:
        raise SQLValidationError(f"SQL failed to parse: {exc}") from exc

    statements = [s for s in statements if s is not None]

    # ── 2. Exactly one statement ───────────────────────────────────────────────
    if len(statements) == 0:
        raise SQLValidationError("Empty SQL query.")

    if len(statements) > 1:
        raise SQLValidationError(
            f"Multiple statements detected ({len(statements)}). "
            "Only a single SELECT is allowed — stacked queries are forbidden."
        )

    stmt = statements[0]

    # ── 3. Top-level statement must be SELECT ──────────────────────────────────
    if not isinstance(stmt, exp.Select):
        kind = type(stmt).__name__
        raise SQLValidationError(
            f"Only SELECT statements are allowed; received '{kind}'."
        )

    # ── 4. No forbidden node types anywhere in the AST ────────────────────────
    #       This catches DML hidden inside CTEs or subqueries.
    for node in stmt.walk():
        if isinstance(node, FORBIDDEN_NODE_TYPES):
            kind = type(node).__name__
            raise SQLValidationError(
                f"Forbidden SQL operation '{kind}' found inside the query. "
                "DML inside CTEs or subqueries is not allowed."
            )

    # ── 5. No forbidden schemas or catalog tables ──────────────────────────────
    for table_node in stmt.find_all(exp.Table):
        db_node = table_node.args.get("db")
        schema = (db_node.name if db_node else "").strip().lower()
        tname  = (table_node.name or "").strip().lower()

        if schema in FORBIDDEN_SCHEMAS:
            raise SQLValidationError(
                f"Access to schema '{schema}' is not allowed."
            )
        if tname in FORBIDDEN_TABLES:
            raise SQLValidationError(
                f"Access to system table '{tname}' is not allowed."
            )

    # ── 6. No forbidden functions ─────────────────────────────────────────────
    #       sqlglot represents unknown functions as exp.Anonymous (name attr holds the fn name).
    #       Known functions are subclasses of exp.Func — check both.
    for func_node in stmt.find_all(exp.Anonymous):
        fname = (func_node.name or "").strip().lower()
        if fname in FORBIDDEN_FUNCTIONS:
            raise SQLValidationError(
                f"Use of function '{fname}' is not allowed."
            )

    for func_node in stmt.find_all(exp.Func):
        # type name maps to the SQL keyword (e.g. PgSleep → pg_sleep isn't modelled,
        # but Anonymous above catches it; this handles any future sqlglot-modelled ones)
        fname = type(func_node).__name__.lower().replace("_", "")
        # Also check the sql name if the class exposes it
        sql_name_attr = getattr(func_node, "sql_name", "")
        sql_name = (sql_name_attr() if callable(sql_name_attr) else sql_name_attr).lower()
        for candidate in (fname, sql_name):
            if candidate in FORBIDDEN_FUNCTIONS:
                raise SQLValidationError(
                    f"Use of function '{candidate}' is not allowed."
                )

    # ── 7. Allowed-table whitelist ────────────────────────────────────────────
    #       Rejects hallucinated table names not present in the retrieved context.
    if allowed_tables is not None:
        allowed_lower = {t.lower() for t in allowed_tables}
        for table_node in stmt.find_all(exp.Table):
            tname = (table_node.name or "").strip().lower()
            # Skip subquery aliases and empty names
            if not tname:
                continue
            if tname not in allowed_lower:
                raise SQLValidationError(
                    f"Table '{tname}' was not found in the retrieved schema context. "
                    "The query may reference a hallucinated or disallowed table."
                )
