import contextlib
import io
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..core import system
from ..core.constants import (
    BLUE,
    GRAY,
    GREEN,
    INFO,
    MARK_NOTE,
    MARK_SECTION,
    PURPLE,
    RESET,
    SUMMARY_RULE_WIDTH,
    THEME_TITLE,
)
from ..core.file_ops import bytes_to_human
from ..core.history import record_history_session
from ..core.scan_cache import ScanCache
from .apps import clean_apps_deep, proactive_app_detection
from .dev import clean_developer_tools
from .system import clean_system_data
from .user import clean_user_data


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
                f"{THEME_TITLE}{MARK_SECTION} System & Package Manager{RESET}",
                [CleanupTask("System & Packages", clean_system_data)],
            ),
            (
                f"{THEME_TITLE}{MARK_SECTION} User Data Cleanup{RESET}",
                [CleanupTask("User Data & Trash", clean_user_data)],
            ),
            (
                f"{THEME_TITLE}{MARK_SECTION} Deep App Cleanup{RESET}",
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
                f"{THEME_TITLE}{MARK_SECTION} Developer Tools & AI Models{RESET}",
                [CleanupTask("Developer Artifacts", clean_developer_tools)],
            ),
        ]


def _print_cleanup_summary(
    dry_run: bool,
    total_size: int,
    total_items: int,
    category_results: list[tuple[str, int, int]],
    interrupted: bool = False,
) -> None:
    """Prints the formatted completion breakdown and disk space summary.

    *interrupted* means the run stopped before its last group -- Ctrl-C, a kill,
    or a task raising. Everything below is still true, it is just not the whole
    run. The summary is printed either way, because the numbers describe files
    that are already gone.
    """
    free_now = shutil.disk_usage(os.path.expanduser("~")).free
    print("\n" + "=" * SUMMARY_RULE_WIDTH)
    if interrupted:
        status_text = "Scan interrupted (Preview)" if dry_run else "Cleanup interrupted"
    else:
        status_text = "Scan complete (Preview)" if dry_run else "Cleanup complete"
    print(f"{BLUE}{status_text}{RESET}")

    if category_results:
        print(f"Breakdown:{RESET}")
        # Reserve two cells for the count so single-digit summaries do not
        # touch the opening parenthesis and multi-digit counts remain aligned.
        count_width = max(2, max(len(str(items)) for _, _, items in category_results))
        item_width = max(
            count_width + 1 + len("item" if items == 1 else "items")
            for _, _, items in category_results
        )
        for name, size, items in category_results:
            noun = "item" if items == 1 else "items"
            item_summary = f"{items:>{count_width}} {noun}".ljust(item_width)
            print(f"  • {name:<25} {GREEN}{bytes_to_human(size):>10}{RESET} ({item_summary})")

    size_label = "\nTotal space freed" if not dry_run else "\nTotal space that can be freed"
    if interrupted:
        size_label += " before the interrupt"
    print(f"{size_label}: {GREEN}{bytes_to_human(total_size)}{RESET} | Items: {total_items}")

    if not dry_run:
        movies = total_size / (8 * 1024 * 1024 * 1024)
        if movies >= 0.1:
            print(f"Equivalent to ~{movies:.1f} 4K movies of storage.")
        print(f"Free space now: {bytes_to_human(free_now)}")

    print("=" * SUMMARY_RULE_WIDTH)
    if interrupted:
        print(
            f"\n{INFO} {GRAY}Stopped before the end: the groups above had already run, the rest never started.{RESET}"
        )
    if dry_run:
        print(f"\n{INFO} {GRAY}Run without --dry-run to actually delete these files.{RESET}")


def run_clean(dry_run: bool = False) -> bool:
    """Orchestrates system, user, app, and developer tool cleanup pipelines.

    False means the cleanup never ran (sudo declined). Individual delete
    failures are reported per line by the sub-cleaners but do not fail the run:
    a cache file another process is holding is expected, not an error the caller
    should act on.
    """
    detected_apps = proactive_app_detection()

    print(f"\n{PURPLE}Clean Your Linux{RESET}\n")
    # The `--dry-run` half is only news to someone who has not used it. Inside a
    # dry run it contradicted the closing line ("Run without --dry-run to
    # actually delete these files."), which was already conditional -- this one
    # simply never asked. The whitelist half is worth saying either way.
    hint = "'topo whitelist --help' for whitelist details."
    hint = f"See {hint}" if dry_run else f"Use 'topo clean --dry-run' to preview, {hint}"
    print(f"{GRAY}{MARK_NOTE} {hint}{RESET}")

    if not system.authenticate_sudo_session(
        dry_run, request_subject="System caches", action="cleanup"
    ):
        return False

    session_command = "clean --dry-run" if dry_run else "clean"
    record_history_session(session_command, "started")

    total_size = 0
    total_items = 0
    category_results: list[tuple[str, int, int]] = []
    execution_groups = TaskRegistry.build_execution_groups(detected_apps)

    # Ctrl-C in the middle of a group used to throw away everything this run had
    # already deleted: the group's own `✓` lines were still sitting in `buf`, the
    # summary never printed, and no closing session line reached the audit log, so
    # `topo history` showed the run as `incomplete` forever. Every report step now
    # runs from a `finally`, and each group flushes whatever it managed to write.
    #
    # `finished` flips on the one path that ran every group, so every other way
    # out reports as a stop: Ctrl-C, SIGTERM (which arrives as SystemExit, not
    # KeyboardInterrupt) and a task raising all leave it False. Claiming "complete"
    # or logging "ended" for those would put a finish in the audit log that never
    # happened.
    finished = False
    try:
        for header, tasks in execution_groups:
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    for task in tasks:
                        size, items, _ = task.action(dry_run=dry_run)
                        if size > 0 or items > 0:
                            total_size += size
                            total_items += items
                            category_results.append((task.name, size, items))
            finally:
                # redirect_stdout has already restored the real stdout by now, so
                # this reaches the terminal even while an exception unwinds.
                output = buf.getvalue()
                if output.strip():
                    print(header)
                    print(output, end="")
        finished = True
    finally:
        _print_cleanup_summary(
            dry_run, total_size, total_items, category_results, interrupted=not finished
        )

        if not dry_run:
            ScanCache.clear()

        record_history_session(session_command, "ended" if finished else "interrupted")

    return True
