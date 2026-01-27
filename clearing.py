import os

cache_path = "/workspace/hf_cache/hub/models--Qwen--Qwen2.5-Coder-14B-Instruct"

print("Checking cache structure...")
print(f"Cache path: {cache_path}\n")

if os.path.exists(cache_path):
    print("✅ Model directory exists\n")
    
    # Calculate total size
    total_size = 0
    file_count = 0
    
    for root, dirs, files in os.walk(cache_path):
        for file in files:
            file_path = os.path.join(root, file)
            try:
                file_size = os.path.getsize(file_path)
                total_size += file_size
                file_count += 1
                
                # Show large files (likely model weights)
                if file_size > 100 * 1024 * 1024:  # > 100 MB
                    size_gb = file_size / 1024**3
                    print(f"  📦 {file}: {size_gb:.2f} GB")
            except:
                pass
    
    print(f"\n📊 Summary:")
    print(f"  Total files: {file_count}")
    print(f"  Total size: {total_size / 1024**3:.2f} GB")
    
    if total_size / 1024**3 > 7:
        print("  ✅ Size looks correct for 14B 4-bit model")
    else:
        print("  ⚠️  Size seems small - download may be incomplete")
else:
    print("❌ Model directory not found!")