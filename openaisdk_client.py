from openai import OpenAI
import os
import time

starting_time = time.time()
# Connect to your local vLLM server
# Note: vLLM is compatible with the OpenAI library!
client = OpenAI(
    base_url="http://localhost:8000/v1", 
    api_key="EMPTY" # vLLM doesn't require a key locally
)

# Your database schema
with open("structure_employee_contacts.sql", "r") as file:
    ddl_context = file.read()

question = "Please provide Mirzə Abbaszadə's registered address and LinkedIn address."

response = client.chat.completions.create(
    model="Qwen/Qwen2.5-Coder-32B-Instruct-AWQ",
    messages=[
        {
            "role": "system", 
            "content": f"""You are an expert PostgreSQL query generator. Your job is to generate the SQL Query 
                        that fetches the relevant data that the user wants. Here is the database structure:
                        {ddl_context}.

                        Rules: 
                            1. Return ONLY THE SQL QUERY. 
                            2. Do not use Markdown formatting.
                            3. Do not explain. Return JUST SQL QUERY."""
        },
        {"role": "user", "content": question}
    ],
    temperature=0.1,
)

print(f"Response generated in {round((time.time()-starting_time), 2)} seconds.")
print(response.choices[0].message.content)