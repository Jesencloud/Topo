import os
import shutil
import subprocess
from pathlib import Path
from ..core.system import run_command
from ..core.file_ops import safe_remove, get_size, bytes_to_human

def clean_tool_cache(description, command_args, cache_path=None, dry_run=False):
    """Helper to clean a specific tool's cache with size reporting."""
    total_size = 0
    if cache_path:
        path = Path(cache_path).expanduser()
        if path.exists():
            total_size = get_size(path)

    if total_size > 0 or dry_run:
        status = "would be cleaned" if dry_run else "cleaned"
        size_info = f" ({bytes_to_human(total_size)})" if total_size > 0 else ""
        print(f"  \033[0;32m✓\033[0m {description}{size_info} {status}")
    
    if dry_run:
        return total_size, 1

    if total_size > 0 or not cache_path:
        res = run_command(command_args, capture=True)
        if res and res.returncode == 0:
            return total_size, 1
    return 0, 0

def safe_clean_glob(pattern_path, description, dry_run=False):
    """Cleans files matching a glob pattern."""
    path = Path(pattern_path).expanduser()
    parent = path.parent
    pattern = path.name
    
    if not parent.exists():
        return 0, 0

    found = list(parent.glob(pattern))
    if not found:
        return 0, 0

    total_size = 0
    for item in found:
        total_size += get_size(item)

    print(f"  \033[0;32m✓\033[0m {description} ({bytes_to_human(total_size)})")
    if dry_run:
        return total_size, len(found)

    for item in found:
        safe_remove(item, use_trash=False)
    return total_size, len(found)

def clean_docker(dry_run=False):
    """Clean unused Docker data."""
    if shutil.which("docker"):
        print("  \033[0;32m✓\033[0m Docker (unused images/volumes)")
        if not dry_run:
            # Check if docker needs sudo
            use_sudo = True
            try:
                res = subprocess.run(["docker", "info"], capture_output=True)
                if res.returncode == 0: use_sudo = False
            except: pass
            
            run_command(["docker", "system", "prune", "-f", "--volumes"], use_sudo=use_sudo, capture=True)
        return 0, 1
    return 0, 0

def clean_ai_models(dry_run=False):
    """Clean heavy AI model hubs (Ollama, Hugging Face, etc.)"""
    total_size = 0
    total_items = 0
    
    # 1. Hugging Face Hub (Massive cached models)
    s, i = safe_clean_glob("~/.cache/huggingface/hub/*", "HuggingFace Model Cache", dry_run=dry_run)
    total_size += s; total_items += i
    
    # 2. Ollama (Local LLMs)
    ollama_path = Path.home() / ".ollama/models"
    if ollama_path.exists():
        size = get_size(ollama_path)
        if size > 0:
            print(f"  \033[0;32m✓\033[0m Ollama Models ({bytes_to_human(size)})")
            if not dry_run:
                for item in ollama_path.iterdir():
                    safe_remove(item, use_trash=False)
            total_size += size; total_items += 1

    # 3. LM Studio & others
    s, i = safe_clean_glob("~/.cache/lm-studio/*", "LM Studio Cache", dry_run=dry_run)
    total_size += s; total_items += i
    
    return total_size, total_items

def clean_developer_tools(dry_run=False):
    print("\033[1;95m➤ Developer Tools & AI Models\033[0m")
    total_size = 0
    total_items = 0
    categories = 0
    
    # 1. Package Manager Caches
    if shutil.which("npm"):
        s, i = clean_tool_cache("npm cache", ["npm", "cache", "clean", "--force"], 
                                cache_path="~/.npm", dry_run=dry_run)
        total_size += s; total_items += i; categories += 1
    
    if shutil.which("pip3"):
        s, i = clean_tool_cache("pip cache", ["pip3", "cache", "purge"], 
                                cache_path="~/.cache/pip", dry_run=dry_run)
        total_size += s; total_items += i; categories += 1

    if shutil.which("go"):
        s, i = clean_tool_cache("go cache", ["go", "clean", "-cache"], 
                                cache_path="~/.cache/go-build", dry_run=dry_run)
        total_size += s; total_items += i; categories += 1

    # 2. IDE Caches
    s, i = safe_clean_glob("~/.cache/JetBrains/*/caches", "JetBrains IDE caches", dry_run=dry_run)
    total_size += s; total_items += i; categories += (1 if i > 0 else 0)
    
    # 3. AI & Large Models
    s, i = clean_ai_models(dry_run=dry_run)
    total_size += s; total_items += i; categories += (1 if i > 0 else 0)

    # 4. Virtualization & Containers
    s, i = clean_docker(dry_run=dry_run)
    total_size += s; total_items += i; categories += (1 if i > 0 else 0)

    return total_size, total_items, categories
