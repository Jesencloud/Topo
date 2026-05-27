import os
import shutil
import subprocess
from pathlib import Path
from ..core.file_ops import safe_remove, get_size, bytes_to_human

def clean_trash(dry_run=False):
    """Empty Linux trash (supports gio and common trash directories)."""
    trash_root = Path.home() / ".local/share/Trash"
    total_cleaned = get_size(trash_root) if trash_root.exists() else 0
    
    if total_cleaned > 0 or dry_run:
        print(f"  \033[0;32m✓\033[0m Trash" + (f" ({bytes_to_human(total_cleaned)})" if total_cleaned > 0 else ""))
    
    if not dry_run and total_cleaned > 0:
        # Method 1: Using gio (Standard)
        if shutil.which("gio"):
            subprocess.run(["gio", "trash", "--empty"], capture_output=True)
        else:
            # Method 2: Manual cleanup
            trash_dirs = [Path.home() / ".local/share/Trash"]
            for trash_root in trash_dirs:
                files_dir = trash_root / "files"
                info_dir = trash_root / "info"
                if files_dir.exists():
                    for item in files_dir.iterdir():
                        safe_remove(item, use_trash=False)
                if info_dir.exists():
                    for item in info_dir.iterdir():
                        safe_remove(item, use_trash=False)
    
    return total_cleaned, (1 if total_cleaned > 0 else 0), 1

from ..clean.apps import is_app_running

def clean_user_caches(dry_run=False):
    """Clean standard Linux user caches (~/.cache)."""
    cache_dir = Path.home() / ".cache"
    if not cache_dir.exists():
        return 0, 0, 0

    targets = [
        ("Thumbnails", cache_dir / "thumbnails", None),
        ("Google Chrome Cache", cache_dir / "google-chrome", ["google-chrome", "chrome"]),
        ("Mozilla Firefox Cache", cache_dir / "mozilla/firefox", ["firefox"]),
        ("VS Code Cache", cache_dir / "Code/Cache", ["code"]),
        ("VS Code CachedData", cache_dir / "Code/CachedData", ["code"]),
    ]
    
    total_size = 0
    total_items = 0
    categories = 0
    for name, path, procs in targets:
        if path.exists():
            # Safety: Check if app is running
            if procs:
                if any(is_app_running(p) for p in procs):
                    print(f"  \033[0;90m◎\033[0m {name} is running · cleanup skipped")
                    continue

            size = get_size(path)
            if size == 0: continue
            categories += 1
            print(f"  \033[0;32m✓\033[0m {name}")
            if not dry_run:
                try:
                    for item in path.iterdir():
                        s = get_size(item)
                        if safe_remove(item, use_trash=False)[0]:
                            total_size += s
                            total_items += 1
                except: pass
            else:
                total_size += size
                total_items += 1
    return total_size, total_items, categories

def clean_system_temp(dry_run=False):
    total_size = 0
    total_items = 0
    for temp_dir in [Path("/tmp"), Path("/var/tmp")]:
        if not temp_dir.exists(): continue
        try:
            import time
            now = time.time()
            for item in temp_dir.iterdir():
                try:
                    if now - item.stat().st_atime < 86400: continue
                    size = get_size(item)
                    if dry_run or safe_remove(item, use_trash=False)[0]:
                        total_size += size
                        total_items += 1
                except: continue
        except: continue
    
    if total_size > 0 or dry_run:
        print(f"  \033[0;32m✓\033[0m Temp files ({bytes_to_human(total_size)})")
        
    return total_size, total_items, 1

def clean_user_data(dry_run=False):
    total_size = 0
    total_items = 0
    categories = 0
    
    s, i, c = clean_trash(dry_run)
    total_size += s; total_items += i; categories += c
    
    s, i, c = clean_user_caches(dry_run)
    total_size += s; total_items += i; categories += c
    
    s, i, c = clean_system_temp(dry_run)
    total_size += s; total_items += i; categories += c
    
    return total_size, total_items, categories
