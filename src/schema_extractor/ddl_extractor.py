import psycopg2
import psycopg2.extras
from dataclasses import dataclass, field

@dataclass
class ColummnInfo:
    """
    Represents one column in a table.
    This is structure of the columns_info JSONB array in metadata DB.

    `description` carries the column comment from pg_description (set via
    `COMMENT ON COLUMN ...`) when present in the target DB. Empty otherwise —
    the LLM-description step or the structural fallback fills it later.
    """
    name: str
    data_type: str
    is_nullable: bool
    column_default: str | None
    is_primary_key: bool = False
    is_unique: bool = False
    description: str = ""

@dataclass
class TableDDL:
    """
    Represents all extracted information for a single table.
    Holds both the structured column list and the reconstructed DDL string.

    `table_description` carries the table comment from pg_description (set via
    `COMMENT ON TABLE ...`) when present in the target DB. Empty otherwise.
    """
    table_name: str
    schema_name: str = 'public'
    columns: list[ColummnInfo] = field(default_factory=list)
    ddl_text: str = ""
    table_description: str = ""

def get_keys(connection: psycopg2.extensions.connection, table_name: str, constraint_type: str='PRIMARY KEY') -> set[str]:
    """
    Returns a set of column names that are part of either primary key or unique for this table.
    """
    query = """
            SELECT kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
            ON tc.constraint_name = kcu.constraint_name
                AND tc.table_schema = kcu.table_schema
            WHERE tc.table_name = %(table)s
                AND tc.table_schema = 'public'
                AND tc.constraint_type = %(constraint_type)s;
        """
    
    with connection.cursor() as cursor:
        cursor.execute(query, {"table": table_name, "constraint_type": constraint_type})
        rows = cursor.fetchall()
    
    return [row['column_name'] for row in rows]

def get_column_comments(
    connection: psycopg2.extensions.connection,
    table_name: str,
    schema_name: str = 'public',
) -> dict[str, str]:
    """
    Returns {column_name: comment} for every column in the table that has a
    non-empty comment in pg_description. Columns without comments are omitted.

    Comments are written via `COMMENT ON COLUMN schema.table.col IS '...';` and
    stored in pg_description (objsubid = column ordinal, > 0).
    """
    query = """
        SELECT a.attname AS column_name,
               pgd.description AS comment
        FROM pg_catalog.pg_attribute a
        JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_description pgd
               ON pgd.objoid = a.attrelid AND pgd.objsubid = a.attnum
        WHERE n.nspname = %(schema_name)s
          AND c.relname = %(table_name)s
          AND a.attnum > 0
          AND NOT a.attisdropped;
    """
    with connection.cursor() as cursor:
        cursor.execute(query, {"schema_name": schema_name, "table_name": table_name})
        rows = cursor.fetchall()

    return {row['column_name']: row['comment'] for row in rows if row['comment']}


def get_table_comment(
    connection: psycopg2.extensions.connection,
    table_name: str,
    schema_name: str = 'public',
) -> str:
    """
    Returns the table-level comment from pg_description (objsubid = 0), or ""
    when none is set. Comments are written via `COMMENT ON TABLE ...`.
    """
    query = """
        SELECT pgd.description AS comment
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_catalog.pg_description pgd
               ON pgd.objoid = c.oid AND pgd.objsubid = 0
        WHERE n.nspname = %(schema_name)s
          AND c.relname = %(table_name)s;
    """
    with connection.cursor() as cursor:
        cursor.execute(query, {"schema_name": schema_name, "table_name": table_name})
        row = cursor.fetchone()

    return (row['comment'] if row and row['comment'] else "")


def get_columns(connection: psycopg2.extensions.connection, table_name: str) -> list[ColummnInfo]:
    """
    Fetches all columns for a table from information_schema.columns and
    enriches each with its pg_description comment (when present).
    """
    query = """
            SELECT
                column_name,
                data_type,
                is_nullable,
                column_default,
                character_maximum_length
            FROM information_schema.columns
            WHERE table_name = %(table_name)s
                AND table_schema = 'public'
            ORDER BY ordinal_position;
        """

    with connection.cursor() as cursor:
        cursor.execute(query, {"table_name": table_name})
        rows = cursor.fetchall()

    pk_cols = get_keys(connection=connection, table_name=table_name, constraint_type='PRIMARY KEY')
    unique_cols = get_keys(connection=connection, table_name=table_name, constraint_type='UNIQUE')
    comments = get_column_comments(connection=connection, table_name=table_name)

    columns = []

    for row in rows:

        col = ColummnInfo(
            name=row['column_name'],
            data_type=row['data_type'],
            is_nullable=row['is_nullable'],
            column_default=row['column_default'],
            is_primary_key=row['column_name'] in pk_cols,
            is_unique=row['column_name'] in unique_cols,
            description=comments.get(row['column_name'], ""),
        )

        columns.append(col)

    return columns

def build_ddl(table_name: str, columns: list[ColummnInfo]) -> str:
    """
    Reconstructes a CREATE TABLE DDL string from the column metadata we extracted.

    This is what gets stored in table_metadata.ddl_text and evantually shown to the LLM.
    """
    lines = []

    for col in columns:
        parts = [col.name, col.data_type]

        if not col.is_nullable:
            parts.append("NOT NULL")
        
        if col.column_default:
            parts.append(f"DEFAULT {col.column_default}")
        
        if col.is_primary_key:
            parts.append("PRIMARY KEY")
        elif col.is_unique:
            parts.append("UNIQUE")
        
        lines.append("    " + " ".join(parts))
    
    columns_block = ",\n".join(lines)

    return f"CREATE TABLE 'public'.{table_name} (\n{columns_block}\n);"

def extract_table_ddl(connection: psycopg2.extensions.connection, table_name: str) -> TableDDL:
    """
    Builds a full TableDDL object: column metadata (with pg_description comments),
    reconstructed CREATE TABLE statement, and the table-level comment.
    """
    columns = get_columns(connection=connection, table_name=table_name)
    ddl_text = build_ddl(table_name=table_name, columns=columns)
    table_description = get_table_comment(connection=connection, table_name=table_name)

    return TableDDL(
        table_name=table_name,
        schema_name='public',
        columns=columns,
        ddl_text=ddl_text,
        table_description=table_description,
    )