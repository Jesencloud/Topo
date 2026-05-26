import os
import shutil
from ..core.system import ensure_sudo_session
from ..core.file_ops import bytes_to_human
from .system import clean_package_manager, clean_journal
from .user import clean_user_data
from .apps import clean_apps_deep
from .dev import clean_developer_tools

# ANSI Colors
CYAN = "\033[1;36m"
GREEN = "\033[1;32m"
YELLOW = "\033[1;33m"
GRAY = "\033[1;90m"
RESET = "\033[0m"

def run_clean(dry_run=False):
    # 1. Prepare categories
    categories = [
        {"name": "Package Manager Cache", "func": clean_package_manager},
        {"name": "System Journal Logs", "func": clean_journal},
        {"name": "User Data & Trash", "func": clean_user_data},
        {"name": "Deep App Caches", "func": clean_apps_deep},
        {"name": "Developer Artifacts", "func": clean_developer_tools}
    ]

    mode_label = "\033[1;36m[PREVIEW]\033[0m" if dry_run else "\033[1;95m[EXECUTING]\033[0m"
    print(f"{mode_label} Starting system cleanup...\n")

    # Pre-authorize sudo to avoid interrupting the progress list
    if not dry_run:
        print(f" {GRAY}🔒 Authorizing system-level tasks...{RESET}")
        if not ensure_sudo_session():
            print(f" {YELLOW}⚠️  Note: Some system caches will be skipped due to lack of permission.{RESET}\n")
        else:
            print(f" {GREEN}✓{RESET} Authorization successful.\n")

    total_size = 0
    total_items = 0
    total_categories = 0

    # 2. Execute all categories one by one
    for cat in categories:
        s, i, c = cat["func"](dry_run=dry_run)
        total_size += s
        total_items += i
        total_categories += c

    # 3. Final Summary
    free_now = shutil.disk_usage(os.path.expanduser("~")).free
    
    print("\n" + "=" * 60)
    status_text = "Scan complete (Preview)" if dry_run else "Cleanup complete"
    print(f"\033[1;34m{status_text}\033[0m")
    
    size_label = "Space that can be freed" if dry_run else "Space freed"
    print(f"{size_label}: \033[1;32m{bytes_to_human(total_size)}\033[0m | Items: {total_items} | Categories: {total_categories}")
    
    if not dry_run:
        movies = total_size / (8 * 1024 * 1024 * 1024)
        if movies >= 0.1:
            print(f"Equivalent to ~{movies:.1f} 4K movies of storage.")
        print(f"Free space now: {bytes_to_human(free_now)}")
    
    print("=" * 60)
    
    if dry_run:
        print(f"\n{GRAY}ℹ️  Run without --dry-run to actually delete these files.{RESET}")
