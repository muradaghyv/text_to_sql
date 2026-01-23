import psycopg2

from dotenv import load_dotenv
import os

if load_dotenv("/home/murad/Documents/self-study/text_to_sql/env/.env"):
    api_key = os.getenv("GEMINI_API_KEY")
    db_ip = os.getenv("DATABASE_IP")
    db_port = os.getenv("DATABASE_PORT")
    db_user = os.getenv("POSTGRES_USER")
    db_name = os.getenv("POSTGRES_DB_NAME")
    db_password = os.getenv("POSTGRES_PASSWORD")

credentials = {
    "database": db_name,
    "user": db_user,
    "password": db_password,
    "host": db_ip,
    "port": db_port
}

try:
    connection = psycopg2.connect(**credentials)
    print("Connection is successfully created!")

except Exception as e:
    print(f"An error occurred: {e}")

finally:
    if connection is not None:
        del connection