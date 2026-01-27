import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
import gc
import os

CUSTOM_CACHE_DIR = "/workspace/hf_cache"  # Replace with your actual path

print(f"Cache directory set to: {CUSTOM_CACHE_DIR}")

# 1. Define the Model ID
model_id = "Qwen/Qwen2.5-Coder-32B-Instruct"

def load_model():
    print(f"Loading {model_id} on RTX 5090...")

    torch.cuda.empty_cache()
    gc.collect()
    
    # 2. Configure 4-bit Quantization (The "shrink ray")
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_compute_dtype=torch.float16,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_use_double_quant=True
    )

    # 3. Load Tokenizer
    tokenizer = AutoTokenizer.from_pretrained(model_id,
                                             trust_remote_code=True,
                                             cache_dir=CUSTOM_CACHE_DIR)
    
    # 4. Load Model
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        quantization_config=bnb_config,
        cache_dir=CUSTOM_CACHE_DIR,
        device_map={"": 0},
        trust_remote_code=True,
        dtype=torch.float16
    )
    
    # 5. Printing memory usage
    print("\n=== GPU Memory Allocation")
    print(f"GPU 0: {torch.cuda.memory_allocated(0) / 1024**3:.2f} GB")
    
    return model, tokenizer

def generate_sql(model, tokenizer, question):
    # A simple prompt to test intelligence
    prompt = f"""You are a helpful coding assistant.
    Here is the question: {question}.
"""
    
    inputs = tokenizer(prompt, return_tensors="pt").to("cuda")
    
    print("🚀 Generating response...")
    outputs = model.generate(
        **inputs, 
        max_new_tokens=200, 
        temperature=0.1
    )
    
    response = tokenizer.decode(outputs[0], skip_special_tokens=True)
    print("\n=== AI Response ===")
    print(response)

if __name__ == "__main__":
    torch.cuda.empty_cache()
    gc.collect()
    model, tokenizer = load_model()
    generate_sql(model, tokenizer, "How can I write a function in Python programming language?")
