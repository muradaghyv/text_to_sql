from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from openai import OpenAI
import psycopg2 # Assuming you are using PostgreSQL
import time

app = FastAPI()

# --- Configuration ---
# 1. Connect to vLLM (The Brain)
vllm_client = OpenAI(
    base_url="http://localhost:8000/v1", 
    api_key="EMPTY"
)

# 2. Database Connection (The Execution Layer)
# WARNING: Use environment variables for passwords in production!
DB_CONFIG = {
    "dbname": "your_db_name",
    "user": "your_db_user",
    "password": "your_db_password",
    "host": "localhost", # Or your RDS/Database IP
    "port": "5432"
}

# --- Data Models ---
class UserRequest(BaseModel):
    prompt: str

class APIResponse(BaseModel):
    original_prompt: str
    generated_sql: str
    data: list
    execution_time: float

# --- Helper Functions ---
def get_table_schema():
    # Optimization: Cache this in memory so you don't read the file every request
    with open("structure_employee_contacts.sql", "r") as file:
        return file.read()

def execute_query(sql_query: str):
    """
    Executes the SQL query against the database.
    CRITICAL SECURITY NOTE: This is a read-only MVP. 
    Ensure your DB user has ONLY SELECT permissions to prevent 'DROP TABLE'.
    """
    try:
        conn = psycopg2.connect(**DB_CONFIG)
        cur = conn.cursor()
        cur.execute(sql_query)
        results = cur.fetchall()
        column_names = [desc[0] for desc in cur.description]
        
        # Convert to list of dicts for JSON response
        clean_results = [dict(zip(column_names, row)) for row in results]
        
        cur.close()
        conn.close()
        return clean_results
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Database Error: {str(e)}")

# --- The Endpoint ---
@app.post("/generate", response_model=APIResponse)
async def generate_and_fetch(request: UserRequest):
    start_time = time.time()
    
    # Step 1: Prepare the Context
    ddl_context = get_table_schema()
    
    # Step 2: Talk to vLLM (Inference)
    try:
        llm_response = vllm_client.chat.completions.create(
            model="Qwen/Qwen2.5-Coder-14B-Instruct-AWQ",
            messages=[
                {
                    "role": "system", 
                    "content": f"""You are a strict PostgreSQL query generator. 
                                Respond with ONLY the raw SQL query. 
                                Do not use Markdown (no ```sql). 
                                Do not add explanations.
                                Context: {ddl_context}"""
                },
                {"role": "user", "content": request.prompt}
            ],
            temperature=0.1,
            max_tokens=200 # Safety limit
        )
        sql_query = llm_response.choices[0].message.content.strip()
        
        # Cleanup: Remove markdown if the model hallucinates it anyway
        sql_query = sql_query.replace("```sql", "").replace("```", "").strip()
        
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"LLM Error: {str(e)}")

    # Step 3: Execute the SQL (Action)
    # Security check: Simple heuristic to block destructive commands
    if not sql_query.upper().startswith("SELECT"):
         raise HTTPException(status_code=400, detail="Security Alert: Only SELECT queries are allowed.")

    # data_results = execute_query(sql_query)
    
    total_time = round(time.time() - start_time, 2)
    
    return {
        "original_prompt": request.prompt,
        "generated_sql": sql_query,
        # "data": data_results,
        "execution_time": total_time
    }