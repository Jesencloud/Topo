import os
import shutil

from ..core.analyze import ScanCache
from ..core.constants import (
    CYAN,
    GRAY,
    GREEN,
    PURPLE,
    RESET,
    YELLOW,
)
from ..core.file_ops import bytes_to_human
from ..core.system import ensure_sudo_session
from .apps import clean_apps_deep, proactive_app_detection
from .dev import clean_developer_tools
from .system import clean_journal, clean_package_manager
from .user import clean_user_data


def run_clean(dry_run=False):
    # 0. Proactive Detection (Auto-Discovery) - Run immediately to build registry
    proactive_app_detection()

    # 1. Prepare categories
    mode_label = f"{CYAN}[PREVIEW]{RESET}" if dry_run else f"{PURPLE}[EXECUTING]{RESET}"
    print(f"{mode_label} Starting system cleanup...\n")

    # Pre-authorize sudo to avoid interrupting the progress list
    if not dry_run:
        print(f" {GRAY}🔒 Authorizing system-level tasks (Ctrl+C to cancel)...{RESET}")
        if not ensure_sudo_session():
            print(f" {YELLOW}⚠️  Cleanup cancelled by user.{RESET}\n")
            return
        else:
            print(f" {GREEN}✓{RESET} Authorization successful.\n")

    total_size = 0
    total_items = 0
    total_categories = 0
    category_results = []

    import contextlib
    import io

    # Define the grouped categories
    execution_groups = [
        (
            "\033[1;95m➤ System & Package Manager\033[0m",
            [
                ("Package Manager Cache", clean_package_manager),
                ("System Journal Logs", clean_journal),
            ],
        ),
        ("\033[1;95m➤ User Data Cleanup\033[0m", [("User Data & Trash", clean_user_data)]),
        ("\033[1;95m➤ Deep App Cleanup\033[0m", [("Deep App Caches", clean_apps_deep)]),
        (
            "\033[1;95m➤ Developer Tools & AI Models\033[0m",
            [("Developer Artifacts", clean_developer_tools)],
        ),
    ]

    for header, tasks in execution_groups:
        f = io.StringIO()
        with contextlib.redirect_stdout(f):
            for cat_name, func in tasks:
                s, i, c = func(dry_run=dry_run)
                total_size += s
                total_items += i
                total_categories += c
                if s > 0 or i > 0:
                    category_results.append((cat_name, s, i))

        output = f.getvalue()
        if output.strip():
            print(header)
            print(output, end="")

    # 3. Final Summary
    free_now = shutil.disk_usage(os.path.expanduser("~")).free
    print("\n" + "=" * 60)
    status_text = "Scan complete (Preview)" if dry_run else "Cleanup complete"
    print(f"\033[1;34m{status_text}\033[0m")

    if category_results:
        print(f"\n{GRAY}Breakdown:{RESET}")
        for name, size, items in category_results:
            print(f"  • {name:<25} \033[1;32m{bytes_to_human(size):>10}\033[0m ({items} items)")

    size_label = "\nTotal space freed" if not dry_run else "\nTotal space that can be freed"
    print(f"{size_label}: \033[1;32m{bytes_to_human(total_size)}\033[0m | Items: {total_items}")

    if not dry_run:
        movies = total_size / (8 * 1024 * 1024 * 1024)
        if movies >= 0.1:
            print(f"Equivalent to ~{movies:.1f} 4K movies of storage.")
        print(f"Free space now: {bytes_to_human(free_now)}")

    print("=" * 60)
    if dry_run:
        print(f"\n{GRAY}ℹ️  Run without --dry-run to actually delete these files.{RESET}")
    else:
        ScanCache.clear()
