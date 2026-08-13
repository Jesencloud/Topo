import contextlib
import io
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

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


@dataclass(frozen=True)
class CleanupTask:
    name: str
    action: Callable[..., tuple[int, int, int]]


class TaskRegistry:
    """Registry and pipeline manager for clean operations."""

    @staticmethod
    def build_execution_groups(
        detected_apps: dict[str, dict[str, Any]],
    ) -> list[tuple[str, list[CleanupTask]]]:
        return [
            (
                f"{THEME_TITLE}➤ System & Package Manager{RESET}",
                [
                    CleanupTask("Package Manager Cache", clean_package_manager),
                    CleanupTask("Orphaned Packages", clean_orphaned_packages),
                    CleanupTask("Old Kernels", clean_old_kernels),
                    CleanupTask("System Journal Logs", clean_journal),
                    CleanupTask("Rotated Log Files", clean_rotated_logs),
                    CleanupTask("Zombie Processes", clean_zombies),
                ],
            ),
            (
                f"{THEME_TITLE}➤ User Data Cleanup{RESET}",
                [CleanupTask("User Data & Trash", clean_user_data)],
            ),
            (
                f"{THEME_TITLE}➤ Deep App Cleanup{RESET}",
                [
                    CleanupTask(
                        "Deep App Caches",
                        lambda dry_run=False: clean_apps_deep(
                            dry_run=dry_run, detected_apps=detected_apps
                        ),
                    )
                ],
            ),
            (
                f"{THEME_TITLE}➤ Developer Tools & AI Models{RESET}",
                [CleanupTask("Developer Artifacts", clean_developer_tools)],
            ),
        ]


def _authenticate_sudo_session(dry_run: bool) -> bool:
    """Pre-authorizes sudo to prevent progress interruptions."""
    if dry_run:
        return True

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

    if not system.ensure_sudo_session(
        f"{PURPLE}➔{RESET} System cleanup requires admin access\n{PURPLE}➔{RESET} Password: "
    ):
        if system.SUDO_CANCELLED:
            print(f" {YELLOW}⚠️  Cleanup cancelled by user.{RESET}", end="")
        else:
            print(f" {RED}✗{RESET} Authorization failed. Cleanup skipped.\n")
        return False

    print(f" {GREEN}✓{RESET} Authorization successful.\n")
    return True


def _print_cleanup_summary(
    dry_run: bool,
    total_size: int,
    total_items: int,
    category_results: list[tuple[str, int, int]],
) -> None:
    """Prints the formatted completion breakdown and disk space summary."""
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


def run_clean(dry_run: bool = False) -> bool | None:
    """Orchestrates system, user, app, and developer tool cleanup pipelines."""
    detected_apps = proactive_app_detection()

    print(f"\n{PURPLE}Clean Your Linux{RESET}\n")
    print(
        f"{GRAY}● Use 'topo clean --dry-run' to preview, 'topo whitelist --help' for whitelist details.{RESET}"
    )

    if not _authenticate_sudo_session(dry_run):
        return False

    session_command = "clean --dry-run" if dry_run else "clean"
    record_history_session(session_command, "started")

    total_size = 0
    total_items = 0
    category_results: list[tuple[str, int, int]] = []
    execution_groups = TaskRegistry.build_execution_groups(detected_apps)

    for header, tasks in execution_groups:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            for task in tasks:
                size, items, _ = task.action(dry_run=dry_run)
                total_size += size
                total_items += items
                if size > 0 or items > 0:
                    category_results.append((task.name, size, items))

        output = buf.getvalue()
        if output.strip():
            print(header)
            print(output, end="")

    _print_cleanup_summary(dry_run, total_size, total_items, category_results)

    if not dry_run:
        ScanCache.clear()

    record_history_session(session_command, "ended")
    return None
