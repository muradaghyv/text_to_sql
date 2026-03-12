import psycopg2
import psycopg2.extras
from dataclasses import dataclass, field

@dataclass
class ColummnInfo:
    """
    Represents one column in a table.
    This is structure of the columns_info JSONB array in metadata DB.
    """
    name: str
    data_type: str
    is_nullable: bool
    column_default: str | None
    is_primary_key: bool = False
    is_unique: bool = False

@dataclass
class TableDDL:
    """
    Represents all extracted information for a single table.
    Holds both the structured column list and the reconstructed DDL string.
    """
    table_name: str
    schema_name: str = 'public'
    columns: list[ColummnInfo] = field(default_factory=list) #TODO ask what field() does
    ddl_text: str = ""

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

def get_columns(connection: psycopg2.extensions.connection, table_name: str) -> list[ColummnInfo]:
    """
    Fetches all columns for a table from information_schema.columns.
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

    columns = []

    for row in rows:

        col = ColummnInfo(
            name=row['column_name'],
            data_type=row['data_type'],
            is_nullable=row['is_nullable'],
            column_default=row['column_default'],
            is_primary_key=row['column_name'] in pk_cols,
            is_unique=row['column_name'] in unique_cols
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
            parts.append(f"DEAFULT {col.column_default}")
        
        if col.is_primary_key:
            parts.append("PRIMARY KEY")
        elif col.is_unique:
            parts.append("UNIQUE")
        
        lines.append("    " + " ".join(parts))
    
    columns_block = ",\n".join(lines)

    return f"CREATE TABLE 'public'.{table_name} (\n{columns_block}\n);"

def extract_table_ddl(connection: psycopg2.extensions.connection, table_name: str) -> TableDDL:
    """
    According to the build_ddl() and get_columns() methods returns a full TableDDL object.
    """
    columns = get_columns(connection=connection, table_name=table_name)

    ddl_text = build_ddl(table_name=table_name, columns=columns)

    return TableDDL(
        table_name=table_name,
        schema_name='public',
        columns=columns,
        ddl_text=ddl_text
    )