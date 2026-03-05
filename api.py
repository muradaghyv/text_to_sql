from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from openai import OpenAI

from contextlib import asynccontextmanager
import re
import time

import asyncpg

from dotenv import load_dotenv
import os

# Selected model
model_name = "cyankiwi/Qwen3-30B-A3B-Instruct-2507-AWQ-4bit"

# Connecting to vLLM model
try:
    client = OpenAI(
        base_url="https://rk70l37j3dpwnh-8000.proxy.runpod.net/v1",
        api_key="EMPTY"
    )
    print("Connected to the model server successfully!")

except Exception as e:
    raise ValueError(f"An error occurred when connecting to the model server!")
    
env_path = "/workspace/text_to_sql/env/.env"
if not os.path.exists(env_path):
    print(f"WARNING: .env file not found at {env_path}")

load_dotenv(env_path)

db_ip = os.getenv("DATABASE_IP")
db_port = os.getenv("DATABASE_PORT", "5432") # Default to 5432 if missing
db_user = os.getenv("POSTGRES_USER")
db_name = os.getenv("POSTGRES_DB_NAME")
db_password = os.getenv("POSTGRES_PASSWORD")

# DEBUG: Print the config (Masking password) to verify IP is loaded
print(f"DEBUG -> Connecting to DB at IP: '{db_ip}', User: '{db_user}', DB: '{db_name}'")

if not db_ip:
    raise ValueError("CRITICAL ERROR: DATABASE_IP is None! Check your .env file.")

database_url = f"postgres://{db_user}:{db_password}@{db_ip}:{db_port}/{db_name}"
# Defining context manager for creating multiple connections to DB for asynchronous read-write
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("App is starting up, creating database connection pool.")
    pool = await asyncpg.create_pool(
        user=db_user,
        password=db_password,
        database=db_name,
        host=db_ip,
        port=db_port
    )

    app.state.pool = pool

    print("Connection pool is created successfully!")

    yield 

    print("App is shutting down, closing database connection pools")

    pool = app.state.pool

    await pool.close()

    print("Connection pool is closed successfully!")

app = FastAPI(lifespan=lifespan)

class UserRequest(BaseModel):
    prompt: str

class APIResponse(BaseModel):
    original_promt: str
    generated_sql: str
    data: list

# Helper function 1 - reading DDL
def get_db_schema():
    try:
        with open("/workspace/text_to_sql/structure_employee_contacts.sql", "r") as file:
            return file.read()
        print("DDL Context read successful!")
    except Exception as e:
        raise ValueError("Error in reading DDL context!")

# Helper Function 2 - formatting generated SQL Query
def format_sql(text: str) -> str:
    text = re.sub(r"```sql|```", "", text, flags=re.IGNORECASE).strip()

    if not text.endswith(";"):
        text += ";"

    return text

async def execute_query(sql_query: str):
    pool = app.state.pool

    async with pool.acquire() as connection:
        try:
            result = await connection.fetch(sql_query)
            return [dict(row) for row in result]
        except Exception as e:
            raise HTTPException(status_code=500, detail="Database error!")
    
@app.post("/generate")
async def generate_answer(user_request: UserRequest):
    ddl_context = get_db_schema()

    try:
        starting_time = time.time()
        print("Generating response according to the given question. . .")
        llm_response = client.chat.completions.create(
            model=model_name,
            messages=[
                {
                    "role": "system",
                    "content": f"""
                    You are an expert PostgreSQL query generator. Your job is to generate the SQL Query 
                    that fetches the relevant data that the user wants. Here is the database structure:
                    {ddl_context}.
        
                    Output formats rules (MANDATORY):
                    - Output must be a single-line or multi-line plain text SQL query.
                    - Do NOT use ``` or ```sql or any markdown.
                    - Do NOT wrap the output in quotes.
                    - The first character of the output MUST be SELECT, INSERT, UPDATE, or DELETE.
                    - The last character MUST be a semicolon (;).
                    - Return NOTHING except the SQL query itself.

                    EXAMPLE:
                    User: "Please provide Mirzə Abbaszadə's registered address and LinkedIn address."
                    Output: SELECT reg_addr, linkedin FROM employee WHERE first_name = 'Mirzə' AND last_name = 'Abbaszadə';
                        """
                },
                {
                    "role": "user",
                    "content": user_request.prompt
                }
            ],
            temperature=0.1
        )
        
        processing_time = time.time() - starting_time
        print("Response was generated successfully!")
        
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"An error occurred when generating a response: {e}")

    sql_query = llm_response.choices[0].message.content
    sql_query = format_sql(text=sql_query)

    if not sql_query.upper().startswith("SELECT"):
        raise HTTPException(status_code=400, detail="Only SELECT queries are allowed!")

    data = await execute_query(sql_query=sql_query)
    
    return {
        "success": True,
        "message": "Query is generated successfully!",
        "processing_time": processing_time,
        "original_prompt": user_request.prompt,
        "generated_sql": sql_query,
        "data": data
    }

    



    
    




















        
