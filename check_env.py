import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

def check_system():
    print("=== System Check ===")
    # 1. Check CUDA
    if torch.cuda.is_available():
        print(f"✅ GPU Detected: {torch.cuda.get_device_name(0)}")
        print(f"✅ VRAM Available: {torch.cuda.get_device_properties(0).total_memory / 1024**3:.2f} GB")
    else:
        print("❌ GPU NOT detected. Check your drivers.")
        return

    # 2. Test 4-bit Loading (Simulating LLM load)
    print("\n=== Library Check ===")
    try:
        # Define 4-bit config
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_compute_dtype=torch.float16,
            bnb_4bit_quant_type="nf4"
        )
        print("✅ BitsAndBytes Config created successfully.")
        print("✅ Ready to load models in 4-bit mode.")
        
    except Exception as e:
        print(f"❌ Error setting up quantization: {e}")

if __name__ == "__main__":
    check_system()