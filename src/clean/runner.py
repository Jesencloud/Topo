import contextlib
import io
import os
import shutil
from functools import partial

from ..core import system, terminal_state
from ..core.constants import (
    BLUE,
    GRAY,
    GREEN,
    PURPLE,
    RED,
    RESET,
    THEME_TITLE,
    YELLOW,
)
from ..core.file_ops import bytes_to_human
from ..core.history import record_history_session
from ..core.scan_cache import ScanCache
from .apps import clean_apps_deep, proactive_app_detection
from .dev import clean_developer_tools
from .system import (
    clean_journal,
    clean_old_kernels,
    clean_orphaned_packages,
    clean_package_manager,
    clean_rotated_logs,
    clean_zombies,
)
from .user import clean_user_data

_read_sudo_choice = terminal_state.read_sudo_choice


def run_clean(dry_run=False):
    # 0. Proactive Detection (Auto-Discovery) - Run once and reuse downstream
    detected_apps = proactive_app_detection()

    # 1. Prepare categories
    print(f"\n{PURPLE}Clean Your Linux{RESET}\n")
    print(
        f"{GRAY}● Use 'topo clean --dry-run' to preview, 'topo whitelist --help' for whitelist details.{RESET}"
    )

    run_system_tasks = True
    # Pre-authorize sudo to avoid interrupting the progress list
    if not dry_run:
        print(
            f"{PURPLE}➔{RESET} System caches need sudo. "
            f"{GREEN}Enter{RESET} continue, {GRAY}Space{RESET} skip:",
            end=" ",
            flush=True,
        )
        choice = _read_sudo_choice()
        print()
        if choice in (" ", "\x1b"):
            return False
        elif not system.ensure_sudo_session(
            f"{PURPLE}➔{RESET} System cleanup requires admin access\n{PURPLE}➔{RESET} Password: "
        ):
            if system.SUDO_CANCELLED:
                print(f" {YELLOW}⚠️  Cleanup cancelled by user.{RESET}", end="")
            else:
                print(f" {RED}✗{RESET} Authorization failed. Cleanup skipped.\n")
            return
        else:
            print(f" {GREEN}✓{RESET} Authorization successful.\n")

    session_command = "clean --dry-run" if dry_run else "clean"
    record_history_session(session_command, "started")

    total_size = 0
    total_items = 0
    total_categories = 0
    category_results = []

    # Define the grouped categories
    execution_groups = []
    if run_system_tasks:
        execution_groups.append(
            (
                f"{THEME_TITLE}➤ System & Package Manager{RESET}",
                [
                    ("Package Manager Cache", clean_package_manager),
                    ("Orphaned Packages", clean_orphaned_packages),
                    ("Old Kernels", clean_old_kernels),
                    ("System Journal Logs", clean_journal),
                    ("Rotated Log Files", clean_rotated_logs),
                    ("Zombie Processes", clean_zombies),
                ],
            )
        )
    execution_groups.extend(
        [
            (f"{THEME_TITLE}➤ User Data Cleanup{RESET}", [("User Data & Trash", clean_user_data)]),
            (
                f"{THEME_TITLE}➤ Deep App Cleanup{RESET}",
                [("Deep App Caches", partial(clean_apps_deep, detected_apps=detected_apps))],
            ),
            (
                f"{THEME_TITLE}➤ Developer Tools & AI Models{RESET}",
                [("Developer Artifacts", clean_developer_tools)],
            ),
        ]
    )

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
    print(f"{BLUE}{status_text}{RESET}")

    if category_results:
        print(f"\n{GRAY}Breakdown:{RESET}")
        for name, size, items in category_results:
            print(f"  • {name:<25} {GREEN}{bytes_to_human(size):>10}{RESET} ({items} items)")

    size_label = "\nTotal space freed" if not dry_run else "\nTotal space that can be freed"
    print(f"{size_label}: {GREEN}{bytes_to_human(total_size)}{RESET} | Items: {total_items}")

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
    record_history_session(session_command, "ended")
