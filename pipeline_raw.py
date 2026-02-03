import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from dotenv import load_dotenv
import os

import gc

import argparse

parser = argparse.ArgumentParser()

parser.add_argument("--prompt", type=str,
                   help="Which data do you want to fetch from the database?")

parser.add_argument("--credentials_path", type=str, default="env/.env",
                    help="Enter the path to the .env file.")

args = parser.parse_args()

question = args.prompt
env_path = args.credentials_path

if load_dotenv(env_path):
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

with open("structure_employee_contacts.sql", "r") as file:
    ddl_context = file.read()
    
cache_dir = "/home/murad/.cache"

model_id = "Qwen/Qwen2.5-Coder-7B-Instruct"

def load_model(model_id: str):
    print(f"Model {model_id} started to be loaded!")

    torch.cuda.empty_cache()
    gc.collect()

    # Configuration of 4-bit Quantization 
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True
    )

    tokenizer = AutoTokenizer.from_pretrained(
        model_id,
        trust_remote_code=True,
        # cache_dir=cache_dir
    )

    # Load model
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        quantization_config=bnb_config,
        # cache_dir=cache_dir,
        device_map={"": 0},
        trust_remote_code=True,
        dtype=torch.float16
    )

    return model, tokenizer

try:
    print("Starting to load the model")
    model, tokenizer = load_model(model_id)
    system_prompt = f"""
        You are an expert PostgreSQL query generator. Your job is to generate the SQL Query 
        that fetches the relevant data that the user asks. Here is the database structure:
        {ddl_context}.
    
        Rules: 
            1. Return ONLY THE SQL QUERY. 
            2. Do not use Markdown formatting.
            3. Do not explain. Return JUST SQL QUERY.
    
        Here is the question: {question}.
    """
    inputs = tokenizer(system_prompt, return_tensors="pt").to("cuda")

    outputs = model.generate(
        **inputs, 
        max_new_tokens=200,
        temperature=0.1
    )
    response = tokenizer.decode(outputs[0][inputs['input_ids'].shape[1]:], skip_special_tokens=True)
    print(f"Question: {question}")
    print(f"{model_id} answer: {response}")

except Exception as e:
    print(f"An error occurred during inference!")

finally:
    del model
    del tokenizer
