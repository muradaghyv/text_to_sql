import torch
import gc
from transformers import AutoTokenizer, AutoModelForCausalLM, BitsAndBytesConfig

model_id = "Qwen/Qwen2.5-Coder-14B-Instruct"

def clear_gpu_memory():
    """
    Thoroughly clears GPU memory by:
    1. Emptying PyTorch's CUDA cache
    2. Running Python's garbage collector
    """
    torch.cuda.empty_cache()  # Releases cached memory back to GPU
    gc.collect()  # Collects Python objects that are no longer referenced
    print("GPU memory cleared")

def print_gpu_memory():
    """
    Shows current GPU memory usage in a readable format
    """
    allocated = torch.cuda.memory_allocated(0) / 1024**3
    reserved = torch.cuda.memory_reserved(0) / 1024**3
    print(f"\n=== GPU Memory Status ===")
    print(f"Allocated: {allocated:.2f} GB")
    print(f"Reserved:  {reserved:.2f} GB")
    print(f"Free:      {24 - reserved:.2f} GB (approximate)")

def load_model():
    """
    Loads the Qwen2.5-Coder-14B model with 4-bit quantization
    
    Quantization Configuration:
    - load_in_4bit: Enables 4-bit precision (reduces memory by ~75%)
    - bnb_4bit_compute_dtype: Uses float16 for computations (faster than float32)
    - bnb_4bit_quant_type: "nf4" = NormalFloat4, optimized for normally distributed weights
    - bnb_4bit_use_double_quant: Quantizes the quantization constants (extra memory savings)
    """
    
    # Clear any existing GPU memory before loading
    clear_gpu_memory()
    print_gpu_memory()
    
    print(f"\nLoading {model_id}...")
    
    # Configure 4-bit quantization
    bnb_config = BitsAndBytesConfig(
        load_in_4bit=True,                      # Load model weights in 4-bit
        bnb_4bit_compute_dtype=torch.float16,   # Compute in FP16 for speed
        bnb_4bit_quant_type="nf4",              # Use NormalFloat4 quantization
        bnb_4bit_use_double_quant=True          # Quantize the quantization scalars
    )
    
    # Load tokenizer (lightweight, ~10MB)
    tokenizer = AutoTokenizer.from_pretrained(
        model_id,
        trust_remote_code=True
    )
    
    # Load model with quantization
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        quantization_config=bnb_config,
        device_map={"": 0},          # Place entire model on GPU 0
        trust_remote_code=True,
        torch_dtype=torch.float16    # Base dtype before quantization
    )
    
    print_gpu_memory()
    print("Model loaded successfully!")
    
    return model, tokenizer

# IMPORTANT: Call this before running load_model() if re-running in a notebook
if __name__ == "__main__":
    # Clear memory first if this is a re-run
    clear_gpu_memory()
    
    # Load the model
    model, tokenizer = load_model()