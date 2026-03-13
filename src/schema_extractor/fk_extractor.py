import psycopg2

from dataclasses import dataclass

@dataclass
class FKRelationship:
    """
    Representing one foregin key constraint.

    Example: 
        employee.contact_id -> contacts.id
        source_table = "employee"
        source_column = "constact_id"
        target_table = "contacts
        target_columns = "id
    """
    source_schema: str
    source_table: str
    source_column: str
    
    target_schema: str
    target_table: str
    target_column: str

    relationship_type: str = 'FOREIGN KEY'

def extract_foreign_keys(connection: psycopg2.extensions.connection) -> list[FKRelationship]:
    """
    Return all foreign key relationshipds from tables in public schema.

    How query works:
        information_schema.referential_constraints: constraint X references constraint Y 
        information_schema.key_column_usage: constraint X lives on column Z of table A

        information_schema.key_column_usage table is used twice in the main query. As because we 
        are trying to build relationships of FKs, the first time we are taking this table for source part,
        the second time we are taking this table for the targetpart.
    """
    query = """
            SELECT 
                kcu_src.table_schema AS source_schema,
                kcu_src.table_name AS source_table,
                kcu_src.column_name AS source_column,
                kcu_tgt.table_schema AS target_schema,
                kcu_tgt.table_name AS target_table,
                kcu_tgt.column_name AS target_column
            FROM information_schema.referential_constraints rc
            JOIN information_schema.key_column_usage kcu_src
                ON kcu_src.constraint_name = rc.constraint_name
                AND kcu_src.constraint_schema = rc.constraint_schema
            JOIN information_schema.key_column_usage kcu_tgt
                ON kcu_tgt.constraint_name = rc.unique_constraint_name
                AND kcu_tgt.constraint_schema = rc.unique_constraint_schema
                AND kcu_tgt.ordinal_position = kcu_src.ordinal_position
            WHERE kcu_src.table_schema = 'public'
            ORDER BY source_table, source_column;
        """
    
    with connection.cursor() as cursor:
        cursor.execute(query)
        rows = cursor.fetchall()
    
    return [
        FKRelationship(
            source_schema=row["source_schema"],
            source_table=row["source_table"],
            source_column=row["source_column"],
            target_schema=row["target_schema"],
            target_table=row["target_table"],
            target_column=row["target_column"]
        ) 
        for row in rows
    ]