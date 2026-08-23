"""The Analyze screen: browse disk usage, drill into directories, delete targets.

Scanning, sizing and deletion all live in the top-level analyze module; this module owns the
navigation loop, the scan-progress painting and the selectors. The scan header
and spinner helpers are here rather than in core because their whole job is
visual -- they decide when a scan is slow enough to deserve a screen, and repaint
without a full clear so drilling into a subdirectory never flashes.
"""

import os
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ...analyze import (
    ANALYZE_RESULT_LIMIT,
    FAST_EXPLORE_ENTRY_LIMIT,
    SCAN_SPINNER_DELAY,
    build_analysis_entry,
    build_linux_insights,
    delete_and_refresh_cache,
    get_age_hint,
    get_fast_explore_data,
    get_old_items_info,
    parallel_scan_sizes,
    should_use_fast_explore,
)
from ...core.constants import BLUE, CYAN, ERASE_BELOW, GRAY, MAGENTA, PURPLE, RESET, YELLOW
from ...core.engine import get_rust_scan_data, get_rust_tree_data, normalize_scan_path
from ...core.file_types import DIRECTORY_ICON, icon_for_entry
from ...core.scan_cache import ScanCache
from ...core.spinner import DEFAULT_SPINNER_FRAMES
from ...core.system import run_command
from ..navigator import AnalyzeSelector, ConfirmSelector, TopFilesSelector


def _confirm_permanent_delete(question: str) -> bool:
    """Put the permanent-delete question to the user.

    analyze decides whether the question is warranted and remembers the
    answer for the batch; all this does is render the dialog.
    """
    return ConfirmSelector(question).run()


def _explore_notice(data: dict[str, Any] | None) -> str:
    if not data or not data.get("is_fast_explore"):
        return ""
    sampled = data.get("preview_sampled_entries", len(data.get("subdirs", {})))
    limit = data.get("preview_entry_limit", FAST_EXPLORE_ENTRY_LIMIT)
    if data.get("preview_truncated"):
        return (
            f"Preview mode: showing first {sampled or limit} direct entries; "
            "folder sizes are not calculated."
        )
    return "Preview mode: direct entries only; folder sizes are not calculated."


def _scan_status_message(scan_reason: str, target_label: str, frame: str) -> str:
    if scan_reason == "refresh":
        return f" {PURPLE}{frame}{RESET} {GRAY}Refreshing analysis on {target_label}...{RESET}"
    if scan_reason == "explore":
        return f" {PURPLE}{frame}{RESET} {GRAY}Opening {target_label}...{RESET}"
    if scan_reason == "insights":
        return f" {PURPLE}{frame}{RESET} {GRAY}Rust Engine: Analyzing Linux insights, please wait . . .{RESET}"
    return (
        f" {PURPLE}{frame}{RESET} {GRAY}Rust Engine: Analyzing disk usage, please wait . . .{RESET}"
    )


def _render_scan_header(view_title: str) -> None:
    # Repaint in place (home + clear-line + erase-below) rather than issuing a
    # full-screen CLEAR_SCREEN. \033[2J blanks the whole screen in a discrete
    # step, so on a sub-view scan that just barely crosses SCAN_SPINNER_DELAY the
    # previous list flashes to black before the title lands -- an intermittent
    # flicker when drilling in from the parent view. Homing and erasing below in
    # one write (the same idiom AnalyzeSelector.render / _write_scrollable_frame
    # use) overwrites the frame with no all-blank intermediate. Vertical
    # placement is unchanged -- home, one blank line, then the title on row 2 --
    # so the handoff to the result list still doesn't shift.
    print(f"\033[H\033[K\n{PURPLE}{view_title}{RESET}{ERASE_BELOW}", flush=True)


def _scan_with_spinner(
    worker, scan_reason: str, target_label: str, view_title: str
) -> dict[str, Any] | None:
    """Run ``worker()`` in a background thread.

    If it finishes within ``SCAN_SPINNER_DELAY`` the scan screen is never
    painted, so fast scans (small dirs / mostly-cached subtrees) hand off to the
    result list with an in-place redraw — exactly like a cache hit, no flash or
    jitter. Only scans slower than the grace period paint the scan header (in
    place, without a full-screen clear) and animate the spinner."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(worker)
        elapsed = 0.0
        header_shown = False
        frame_index = 0
        last_len = 0
        try:
            while not future.done():
                if not header_shown and elapsed >= SCAN_SPINNER_DELAY:
                    _render_scan_header(view_title)
                    header_shown = True
                if header_shown:
                    msg = _scan_status_message(
                        scan_reason,
                        target_label,
                        DEFAULT_SPINNER_FRAMES[frame_index % len(DEFAULT_SPINNER_FRAMES)],
                    )
                    last_len = max(last_len, len(msg))
                    print(msg, end="\r", flush=True)
                    frame_index += 1
                time.sleep(0.05)
                elapsed += 0.05
            return future.result()
        finally:
            if header_shown:
                print(" " * last_len, end="\r", flush=True)


def _get_rust_scan_data_with_spinner(
    path: Path, scan_reason: str, target_label: str, view_title: str
) -> dict[str, Any] | None:
    return _scan_with_spinner(
        lambda: get_rust_scan_data(path), scan_reason, target_label, view_title
    )


def _fast_explore_with_spinner(
    path: Path, scan_reason: str, target_label: str, view_title: str
) -> dict[str, Any] | None:
    return _scan_with_spinner(
        lambda: get_fast_explore_data(path), scan_reason, target_label, view_title
    )


def _open_in_file_manager(path: Path) -> None:
    """Open the parent directory of *path* in the system file manager."""
    target = path.parent if path.is_file() else path
    run_command(["xdg-open", str(target)], capture=True, timeout=5)


def run_deep_analysis(target_path: Path | None = None):
    # State Stack stores: {"target": Path, "results": [], "data": {}, "total_size": int}
    state_stack: list[dict[str, Any]] = []

    # Current active state
    current_target: Path | None = normalize_scan_path(target_path) if target_path else None
    results: list[dict[str, Any]] = []
    data: dict[str, Any] | None = None
    total_scan_size = 0
    needs_scan = True
    scan_reason = "scan"
    selected_index = 0
    current_page = 0

    while True:
        target_to_scan = current_target or normalize_scan_path(Path.home())
        view_title = " Analyze Disk" if current_target is None else f" Exploring: {current_target}"

        if needs_scan:
            target_label = target_to_scan.name if current_target else "Home"
            if current_target is not None:
                if should_use_fast_explore(target_to_scan):
                    data = _fast_explore_with_spinner(
                        target_to_scan,
                        "refresh" if scan_reason == "refresh" else "explore",
                        target_label,
                        view_title,
                    )
                else:
                    data = _get_rust_scan_data_with_spinner(
                        target_to_scan, scan_reason, target_label, view_title
                    )
            else:
                cached = ScanCache.get(target_to_scan)
                if cached is not None:
                    # Cache hit: load instantly without painting the scan screen,
                    # so the view doesn't blank/flash and shift vertically.
                    data = cached
                else:
                    data = _scan_with_spinner(
                        lambda path=target_to_scan: get_rust_tree_data(path),
                        scan_reason,
                        target_label,
                        view_title,
                    )
            if not data:
                data = get_fast_explore_data(target_to_scan)
            if not data:
                print("\n   ❌ Engine scan failed.")
                time.sleep(1.5)
                if state_stack:
                    prev = state_stack.pop()
                    current_target = prev["target"]
                    results = prev["results"]
                    data = prev["data"]
                    total_scan_size = prev["total_size"]
                    selected_index = prev.get("selected_index", 0)
                    current_page = prev.get("current_page", 0)
                    needs_scan = False
                    continue
                else:
                    break

            total_scan_size = data.get("total_size_bytes", 0)
            results = []

            if current_target is None:
                # Root View: Standard Categories
                total_used = shutil.disk_usage("/").used or 1
                targets: list[dict[str, Any]] = [
                    {"name": "Home", "path": Path.home(), "color": CYAN},
                    {
                        "name": "Applications",
                        "path": Path("/usr/share/applications"),
                        "color": MAGENTA,
                    },
                    {"name": "System", "path": Path("/usr"), "color": BLUE},
                ]

                # --- LINUX INSIGHTS: Detect hidden space killers ---
                home = Path.home()
                insights: list[dict[str, Any]] = build_linux_insights(home)

                # Collect every path that needs a Rust scan and run them concurrently.
                # Home is already scanned (total_scan_size); smart views use a Python
                # age-filter instead of a full scan.
                rust_paths = [
                    t["path"]
                    for t in targets
                    if t["path"].exists() and t["path"] != home and str(t["path"]) != "/"
                ]
                rust_paths += [
                    ins["path"]
                    for ins in insights
                    if ins["path"].exists() and not ins.get("is_smart")
                ]
                scan_sizes = (
                    _scan_with_spinner(
                        lambda paths=rust_paths: parallel_scan_sizes(paths),
                        "insights",
                        "Linux insights",
                        view_title,
                    )
                    or {}
                )

                # A large secondary tree such as /usr can exceed the LRU entry
                # limit by itself and evict Home even though Home was just scanned.
                # Keep the root result hot so returning to the main menu and opening
                # Analyze again in the same process does not repeat the full scan.
                if not data.get("is_fast_explore"):
                    ScanCache.set(target_to_scan, data)

                for t in targets:
                    if t["path"].exists():
                        if t["path"] == home:
                            size = total_scan_size
                        elif str(t["path"]) == "/":
                            size = total_used
                        else:
                            size = scan_sizes.get(t["path"], 0)
                        results.append(
                            {
                                "name": t["name"],
                                "path": t["path"],
                                "size": size,
                                "percent": (size / total_used) * 100,
                                "color": t["color"],
                                "icon": "📊" if str(t["path"]) == "/" else DIRECTORY_ICON,
                                "age_hint": get_age_hint(t["path"]),
                            }
                        )

                for ins in insights:
                    p = ins["path"]
                    if p.exists():
                        smart_items = []
                        if ins.get("is_smart"):
                            # For smart views, we pre-calculate filtered items
                            smart_items = get_old_items_info(p)
                            size = sum(item["size"] for item in smart_items)
                        else:
                            size = scan_sizes.get(p, 0)

                        min_display_bytes = ins.get("min_display_bytes", 10 * 1024 * 1024)
                        if size > min_display_bytes:  # Only show large entries to keep Root clean
                            results.append(
                                {
                                    "name": ins["name"],
                                    "path": p,
                                    "size": size,
                                    "percent": (size / total_used) * 100,
                                    "color": YELLOW,
                                    "icon": ins.get("icon", "👀"),
                                    "age_hint": get_age_hint(p),
                                    "is_smart": ins.get("is_smart"),
                                    "smart_items": smart_items,
                                }
                            )

                # Ensure total_scan_size matches the disk usage baseline for root view
                total_scan_size = total_used
            else:
                total_path_size = total_scan_size or 1
                subdir_map = data.get("subdirs", {})
                entry_meta = data.get("entry_meta", {})
                is_fast_explore = data.get("is_fast_explore", False)
                for name, size in subdir_map.items():
                    full_path = current_target / name
                    meta = entry_meta.get(name, {})
                    if is_fast_explore and meta:
                        is_dir = meta.get("is_dir", False)
                        size_known = meta.get("size_known", True)
                        entry = {
                            "name": name,
                            "path": full_path,
                            "size": size,
                            "percent": (size / total_path_size) * 100 if size_known else 0.0,
                            "icon": icon_for_entry(name, is_dir=is_dir),
                            "size_known": size_known,
                            "sort_group": 0 if is_dir else 1,
                        }
                    else:
                        entry = build_analysis_entry(name, full_path, size, total_path_size)
                    results.append(entry)
                if is_fast_explore:
                    results.sort(key=lambda x: (x.get("sort_group", 1), x["name"].lower()))
                else:
                    results.sort(key=lambda x: x["size"], reverse=True)
                    results = results[:ANALYZE_RESULT_LIMIT]
            needs_scan = False
            scan_reason = "scan"

        selector = AnalyzeSelector(
            view_title,
            results,
            can_select=(current_target is not None),
            notice=_explore_notice(data),
            sort_mode="name" if data and data.get("is_fast_explore") else "size",
        )
        # Restore the cursor/page belonging to this view.  Parent views keep
        # these values on the navigation stack while a child directory is open.
        selector.selected_index = min(selected_index, max(0, len(results) - 1))
        selector.current_page = current_page
        action, idx = selector.run()
        if isinstance(selector.selected_index, int):
            selected_index = selector.selected_index
        if isinstance(selector.current_page, int):
            current_page = selector.current_page

        if action == "QUIT":
            break
        elif action == "BACK":
            if state_stack:
                prev = state_stack.pop()
                current_target = prev["target"]
                results = prev["results"]
                data = prev["data"]
                total_scan_size = prev["total_size"]
                selected_index = prev.get("selected_index", 0)
                current_page = prev.get("current_page", 0)
                # Recalculate parent percentages to reflect any deletions done in child
                if total_scan_size > 0:
                    for r in results:
                        r["percent"] = (r["size"] / total_scan_size) * 100
                needs_scan = False
            else:
                break
        elif action == "REFRESH":
            ScanCache.discard(target_to_scan)
            needs_scan = True
            scan_reason = "refresh"
        elif action == "OPEN":
            path = results[idx]["path"]
            _open_in_file_manager(path)
        elif action == "DRILL_DOWN":
            item = results[idx]
            if item.get("is_smart"):
                # For smart views, show a file list of the filtered items
                top_selector = TopFilesSelector(f"Smart View: {item['name']}", item["smart_items"])
                selected_idxs = top_selector.run()
                if selected_idxs:
                    paths = [item["smart_items"][s_idx]["path"] for s_idx in selected_idxs]
                    if delete_and_refresh_cache(
                        paths, current_target, ask_permanent=_confirm_permanent_delete
                    ):
                        needs_scan = True
                        scan_reason = "refresh"
            elif item["path"].is_dir():
                # Safety: Avoid entering / as it's too heavy and requires sudo for full scan
                if str(item["path"]) == "/":
                    continue

                state_stack.append(
                    {
                        "target": current_target,
                        "results": results,
                        "data": data,
                        "total_size": total_scan_size,
                        "selected_index": selected_index,
                        "current_page": current_page,
                    }
                )
                current_target = item["path"]
                selected_index = 0
                current_page = 0
                needs_scan = True
                scan_reason = "scan"
            elif item["path"].is_file():
                p = item["path"]
                archive_exts = {
                    ".zip",
                    ".tar",
                    ".gz",
                    ".xz",
                    ".bz2",
                    ".7z",
                    ".rar",
                    ".deb",
                    ".rpm",
                    ".apk",
                }
                is_archive = p.suffix.lower() in archive_exts
                is_exec = os.access(p, os.X_OK)
                # .desktop entries can launch arbitrary actions through xdg-open,
                # so treat them like executables and reveal the parent instead.
                is_launchable = p.suffix.lower() == ".desktop"

                if is_archive or is_exec or is_launchable:
                    # Open parent directory instead for safety
                    _open_in_file_manager(p)
                else:
                    run_command(["xdg-open", str(p)], capture=True, timeout=10)
        elif action == "DELETE_BATCH":
            selected_idxs = idx  # action was DELETE_BATCH, idx contains the list
            paths = [results[s_idx]["path"] for s_idx in selected_idxs]
            if delete_and_refresh_cache(
                paths, current_target, ask_permanent=_confirm_permanent_delete
            ):
                needs_scan = True
                scan_reason = "refresh"
        elif action == "OPEN_BATCH":
            selected_idxs = idx
            for s_idx in selected_idxs:
                p = results[s_idx]["path"]
                _open_in_file_manager(p)
