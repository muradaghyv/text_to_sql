from google import genai

import psycopg2

from dotenv import load_dotenv
import os

import argparse

parser = argparse.ArgumentParser()

parser.add_argument("--prompt", type=str,
                    help="Which data do you want to fetch from the database?")
args = parser.parse_args()

question = args.prompt

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

with open("structure_employee_contact.sql", "r") as file:
    ddl_context = file.read()

try:
    print("Gemini Client is created. . .")
    client = genai.Client(api_key=api_key)
    print("Gemini client is successfully created!")

except Exception as e:
    print(f"Error in Gemini Client creation: {e}")

response = client.models.generate_content(
    model="gemini-2.5-flash",
    contents=f"""
    You are an expert PostgreSQL query generator. Your job is to generate the SQL Query 
    that fetches the relevant data that the user wants. Here is the database structure:
    {ddl_context}.

    Rules: Return ONLY THE SQL QUERY. Do not use Markdown formatting or do not explain. JUST SQL QUERY.

    Here is the question: {question}.
"""
)
query = response.text

try:
    connection = psycopg2.connect(**credentials)
    print("Connection is successfully created!")
    cursor = connection.cursor()
    cursor.execute(query)
    result = cursor.fetchall()[0]
    
    print(f"Question: {question}")
    print(f"Answer: {result}")

except Exception as e:
    print(f"An error occurred: {e}")

finally:
    if connection is not None:
        del connection