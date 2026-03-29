import psycopg2
import psycopg2.extras

from dotenv import load_dotenv
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DEFAULT_ENV = os.path.join(_PROJECT_ROOT, "env", ".env")

def get_connection(env_path: str = _DEFAULT_ENV):
    """
    Connects to a PostgreSQL DB and returns psycopg2 instance.

    cursor_factory=RealDictCursor at the connection level so that
    all cursors created from this connection return rows as dicts by default.
    For example, each row returns values of 2 columns. row[0] shows the value of 
    the 1st column, whereas row[1] shows the value of the 2nd column. By returning 
    values as RealDictCursor, we do not need to write row[0] for showing the value of
    the 1st column, we just write row[column_name].
    """
    if load_dotenv(env_path):
        credentials = {
            "database": os.getenv("POSTGRES_DB_NAME"),
            "user": os.getenv("POSTGRES_USER"),
            "password": os.getenv("POSTGRES_PASSWORD"),
            "host": os.getenv("DATABASE_IP"),
            "port": os.getenv("DATABASE_PORT")
        }
    
    else:
        raise ValueError(f"Couldn't find the credentials!")
    
    try:
        connection = psycopg2.connect(
            **credentials,
            cursor_factory=psycopg2.extras.RealDictCursor
        )

        return connection
    
    except Exception as e:
        raise ValueError(f"Error connecting to a database: {e}")

def list_table_names(connection: psycopg2.extensions.connection):
    query = """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = 'public'
                AND table_type = 'BASE TABLE'
            ORDER BY table_name;
        """
    
    with connection.cursor() as cursor:
        cursor.execute(query=query)
        rows = cursor.fetchall()
    
    return [row["table_name"] for row in rows]