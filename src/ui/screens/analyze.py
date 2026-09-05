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
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, TypeVar

from ...analyze import (
    ANALYZE_RESULT_LIMIT,
    FAST_EXPLORE_ENTRY_LIMIT,
    SCAN_SPINNER_DELAY,
    DeleteOutcome,
    FastExploreResult,
    build_analysis_entry,
    build_linux_insights,
    delete_and_refresh_cache,
    filesystem_used_bytes,
    get_age_hint,
    get_fast_explore_data,
    get_old_items_info,
    parallel_scan_sizes,
    percent_of,
)
from ...core.constants import (
    BLUE,
    CYAN,
    ERASE_BELOW,
    FAIL,
    GRAY,
    GREEN,
    MAGENTA,
    OK,
    PURPLE,
    RESET,
    WARN,
    YELLOW,
)
from ...core.engine import get_rust_scan_data, get_rust_tree_data, normalize_scan_path
from ...core.file_types import DIRECTORY_ICON, icon_for_entry
from ...core.render import bytes_to_human
from ...core.scan_cache import ScanCache, ScanResult
from ...core.spinner import DEFAULT_SPINNER_FRAMES
from ...core.system import run_command
from ...core.text import plural
from ..navigator import (
    AnalyzeSelector,
    ConfirmSelector,
    Navigator,
    TopFilesSelector,
    notice_line,
)


def _confirm_permanent_delete(question: str) -> bool:
    """Put the permanent-delete question to the user.

    analyze decides whether the question is warranted and remembers the
    answer for the batch; all this does is render the dialog.
    """
    return ConfirmSelector(question).run()


# Three names for the three kinds of notice this screen raises. The clamp and the
# format live in navigator, next to the frames whose geometry they protect -- the
# selectors there raise notices of their own now.
def _done_notice(text: str) -> str:
    return notice_line(OK, text)


def _warn_notice(text: str) -> str:
    return notice_line(WARN, text)


def _fail_notice(text: str) -> str:
    return notice_line(FAIL, text)


def _delete_notice(outcome: DeleteOutcome) -> str:
    """Describe a finished delete batch in one line.

    Success used to have no feedback at all -- only the delete sound, which says
    nothing on a muted laptop or over ssh -- and the failures printed straight to
    the terminal, where the next frame overwrote them within the same tick. The
    line the frame draws for it sits under the key hints, so putting one up costs
    the list no rows.
    """
    freed = bytes_to_human(outcome.freed_bytes)
    # Count and size are the pair the uninstall report colours, and they get the
    # same green here: it is the same statement of what a removal just freed, so
    # the two screens should not read as two different kinds of report. The words
    # around them stay plain -- the glyph is what says how the batch went.
    tally = f"Deleted {GREEN}{plural(outcome.deleted, 'item')}{RESET}, freed {GREEN}{freed}{RESET}"
    if outcome.deleted and not outcome.failed:
        return _done_notice(f"{tally}.")
    if outcome.deleted:
        return _warn_notice(f"{tally}; {outcome.failed} left: {outcome.first_problem}")
    if outcome.failed:
        return _fail_notice(
            f"{plural(outcome.failed, 'item')} not deleted: {outcome.first_problem}"
        )
    # Nothing was attempted: the admin prompt was declined, or the selection was
    # empty. first_problem carries the reason when there is one.
    return _fail_notice(outcome.first_problem)


def _explore_notice(data: ScanResult | None) -> str:
    if not data or not data.get("is_fast_explore"):
        return ""
    sampled = data.get("preview_sampled_entries", len(data.get("subdirs", {})))
    limit = data.get("preview_entry_limit", FAST_EXPLORE_ENTRY_LIMIT)
    if data.get("preview_truncated"):
        return _warn_notice(
            f"Preview mode: showing first {sampled or limit} direct entries; "
            "folder sizes are not calculated."
        )
    return _warn_notice("Preview mode: direct entries only; folder sizes are not calculated.")


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


# What the worker handed to _scan_with_spinner came back with. A type variable
# rather than one record type: the same helper runs a scan, a preview listing and
# the concurrent sizing pass, and each caller keeps the type it asked for.
T = TypeVar("T")


def _scan_with_spinner(
    worker: Callable[[], T], scan_reason: str, target_label: str, view_title: str
) -> T:
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
) -> ScanResult | None:
    return _scan_with_spinner(
        lambda: get_rust_scan_data(path), scan_reason, target_label, view_title
    )


def _fast_explore_with_spinner(
    path: Path,
    scan_reason: str,
    target_label: str,
    view_title: str,
    *,
    only_when_wide: bool = False,
) -> FastExploreResult | None:
    return _scan_with_spinner(
        lambda: get_fast_explore_data(
            path, FAST_EXPLORE_ENTRY_LIMIT, only_when_wide=only_when_wide
        ),
        scan_reason,
        target_label,
        view_title,
    )


XDG_OPEN_MISSING_NOTICE = "Could not open it: install xdg-utils (xdg-open) to open files here."

# Both the Rust engine and the Python fallback came back empty: the directory is
# unreadable, or the engine is not working at all.
ENGINE_SCAN_FAILED_NOTICE = "Engine scan failed: nothing could be read from that location."


def _open_path(path: Path, timeout: int = 5) -> str:
    """Hand *path* to the desktop opener, returning a notice when that failed.

    Debian's minimal and server images ship without xdg-utils, where pressing
    "open" used to do nothing at all with no explanation. stdin is detached
    because a session without a desktop falls through to a terminal handler that
    would otherwise read the keys this screen is waiting for.
    """
    res = run_command(["xdg-open", str(path)], capture=True, timeout=timeout, detach_stdin=True)
    return "" if res.ok else _warn_notice(XDG_OPEN_MISSING_NOTICE)


def _open_in_file_manager(path: Path) -> str:
    """Open the parent directory of *path* in the system file manager."""
    target = path.parent if path.is_file() else path
    return _open_path(target)


@dataclass
class _View:
    """The view on screen: what is being looked at, and where the cursor is in it.

    Six fields rather than six locals, because they only ever move as a unit.
    Drilling into a directory pushes a copy onto the navigation stack and edits
    the original, so coming back is one rebinding instead of six assignments that
    could restore five fields and forget the sixth. The cursor and page live here
    for exactly that reason: they were added later, so that returning from a child
    lands where it left rather than at the top, and the version before this one
    read them back off the stack with a ``.get(key, 0)`` whose default advertised
    a case the single push site guarantees cannot happen.

    A snapshot shares ``results`` and ``data`` rather than copying them. The
    parent's rows are the same dicts it recalculates percentages on when the
    child returns, and copying a scan of /usr just to push it would buy nothing.
    """

    target: Path | None
    results: list[dict[str, Any]] = field(default_factory=list)
    data: ScanResult | None = None
    total_size: int = 0
    selected_index: int = 0
    current_page: int = 0

    def snapshot(self) -> "_View":
        """The copy that waits on the navigation stack while a child view is open."""
        return replace(self)


def _acquire_view_data(
    current_target: Path | None,
    target_to_scan: Path,
    scan_reason: str,
    target_label: str,
    view_title: str,
) -> ScanResult | None:
    """Read this target's contents, trying up to three sources in turn.

    A sub-view asks the preview pass first, which answers only when the
    directory really is too wide to size child by child, and otherwise hands the
    target to a full Rust scan. The root view reads the shared cache before it
    scans anything, so returning to the main menu and opening Analyze again in
    the same process costs nothing.

    Whichever route was taken, an empty result gets one last try as a plain
    direct listing: that arm is what covers the engine being absent or refusing
    a directory outright. Coming back empty from here means nothing could read
    the target at all, which is the caller's cue to back out of the view.
    """
    data: ScanResult | None
    if current_target is not None:
        data = _fast_explore_with_spinner(
            target_to_scan,
            "refresh" if scan_reason == "refresh" else "explore",
            target_label,
            view_title,
            only_when_wide=True,
        )
        if data is None:
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
                lambda: get_rust_tree_data(target_to_scan),
                scan_reason,
                target_label,
                view_title,
            )
    return data or get_fast_explore_data(target_to_scan)


def _view_row(
    name: str, path: Path, size: int, color: str, icon: str, **extra: Any
) -> dict[str, Any]:
    """One row of the root view.

    Its share is measured against the filesystem the row lives on rather than
    against the view's total: /usr and Home are often different mounts, and the
    row carries the base it was measured with so that a later recalculation --
    coming back from a child directory -- cannot reuse the wrong one.
    """
    base = filesystem_used_bytes(path)
    return {
        "name": name,
        "path": path,
        "size": size,
        "percent": percent_of(size, base),
        "percent_base": base,
        "color": color,
        "icon": icon,
        "age_hint": get_age_hint(path),
        **extra,
    }


def _root_category_rows(
    targets: list[dict[str, Any]], home: Path, home_size: int, scan_sizes: dict[Any, int]
) -> list[dict[str, Any]]:
    """The standard places, skipping any this system does not have.

    Home's size is the scan the view already paid for; the others come from the
    concurrent scan the caller ran for exactly that reason.
    """
    return [
        _view_row(
            t["name"],
            t["path"],
            home_size if t["path"] == home else scan_sizes.get(t["path"], 0),
            t["color"],
            DIRECTORY_ICON,
        )
        for t in targets
        if t["path"].exists()
    ]


def _insight_rows(
    insights: list[dict[str, Any]], scan_sizes: dict[Any, int]
) -> list[dict[str, Any]]:
    """The insight rows worth drawing: the ones that exist and are big enough.

    A smart view is sized by the same age filter that supplies its file list,
    everything else by the concurrent scan. The size threshold is what keeps the
    root view short -- an insight that turned up a few megabytes is not what this
    screen is for -- and each insight may raise its own.
    """
    rows: list[dict[str, Any]] = []
    for ins in insights:
        p = ins["path"]
        if not p.exists():
            continue
        smart_items: list[dict[str, Any]] = []
        if ins.get("is_smart"):
            # For smart views, we pre-calculate filtered items
            smart_items = get_old_items_info(p)
            size = sum(item["size"] for item in smart_items)
        else:
            size = scan_sizes.get(p, 0)

        if size > ins.get("min_display_bytes", 10 * 1024 * 1024):
            rows.append(
                _view_row(
                    ins["name"],
                    p,
                    size,
                    YELLOW,
                    ins.get("icon", "👀"),
                    is_smart=ins.get("is_smart"),
                    smart_items=smart_items,
                )
            )
    return rows


def _root_view_results(
    data: ScanResult, target_to_scan: Path, home_size: int, view_title: str
) -> tuple[list[dict[str, Any]], int]:
    """The root view's rows, and the total they are drawn against.

    That total is the filesystem's used bytes rather than the sum of the rows:
    the categories overlap each other and every insight sits inside one of them,
    so adding them up would describe a disk nobody has.
    """
    total_used = shutil.disk_usage("/").used or 1
    home = Path.home()
    targets: list[dict[str, Any]] = [
        {"name": "Home", "path": home, "color": CYAN},
        {
            "name": "Applications",
            "path": Path("/usr/share/applications"),
            "color": MAGENTA,
        },
        {"name": "System", "path": Path("/usr"), "color": BLUE},
    ]
    insights: list[dict[str, Any]] = build_linux_insights(home)

    # Collect every path that needs a Rust scan and run them concurrently.
    # Home is already scanned (home_size); smart views use a Python
    # age-filter instead of a full scan.
    rust_paths = [
        t["path"]
        for t in targets
        if t["path"].exists() and t["path"] != home and str(t["path"]) != "/"
    ]
    rust_paths += [
        ins["path"] for ins in insights if ins["path"].exists() and not ins.get("is_smart")
    ]
    scan_sizes = _scan_with_spinner(
        lambda: parallel_scan_sizes(rust_paths), "insights", "Linux insights", view_title
    )

    # A large secondary tree such as /usr can exceed the LRU entry
    # limit by itself and evict Home even though Home was just scanned.
    # Keep the root result hot so returning to the main menu and opening
    # Analyze again in the same process does not repeat the full scan.
    # Unconditional: ScanCache.set turns a preview listing away itself.
    ScanCache.set(target_to_scan, data)

    rows = _root_category_rows(targets, home, home_size, scan_sizes)
    rows += _insight_rows(insights, scan_sizes)
    return rows, total_used


def _child_view_results(
    current_target: Path, data: Mapping[str, Any], total_size: int
) -> list[dict[str, Any]]:
    """One directory's entries, drawn as shares of that directory.

    A real scan is ranked first and built second: every row costs ~10 syscalls of
    cache and age metadata, and only the ones that survive the cut are ever
    drawn. Ranking needs nothing but the sizes the scan already returned, so this
    is also the order the view is drawn in -- there is no second sort afterwards.

    Preview data is a listing rather than a measurement, so it is kept whole and
    sorted by name with directories first: ordering entries whose size was never
    computed by that size would rank them by an answer nobody has.

    Takes a plain mapping rather than a ScanResult because it is the one place
    that draws either record and reads the keys only analyze.FastExploreResult
    has -- every read here carries the default that covers the other kind.
    """
    total_path_size = total_size or 1
    subdir_map = data.get("subdirs", {})
    entry_meta = data.get("entry_meta", {})
    is_fast_explore = data.get("is_fast_explore", False)
    if is_fast_explore:
        ranked = list(subdir_map.items())
    else:
        ranked = sorted(subdir_map.items(), key=lambda item: item[1], reverse=True)[
            :ANALYZE_RESULT_LIMIT
        ]
    rows: list[dict[str, Any]] = []
    for name, size in ranked:
        full_path = current_target / name
        meta = entry_meta.get(name, {})
        if is_fast_explore and meta:
            is_dir = meta.get("is_dir", False)
            size_known = meta.get("size_known", True)
            rows.append(
                {
                    "name": name,
                    "path": full_path,
                    "size": size,
                    "percent": percent_of(size, total_path_size) if size_known else 0.0,
                    "percent_base": total_path_size,
                    "icon": icon_for_entry(name, is_dir=is_dir),
                    "size_known": size_known,
                    "sort_group": 0 if is_dir else 1,
                }
            )
        else:
            rows.append(build_analysis_entry(name, full_path, size, total_path_size))
    if is_fast_explore:
        rows.sort(key=lambda x: (x.get("sort_group", 1), x["name"].lower()))
    return rows


def _delete_selection(paths: list[Path], current_target: Path | None) -> tuple[str, bool]:
    """Delete a chosen batch, returning its notice and whether to rescan.

    Both call sites -- a row selection and a smart view's file list -- need the
    same three things in the same order, and the last is the easy one to forget:
    a batch that deleted nothing must not trigger a rescan, or declining the
    admin prompt would rebuild the whole view for no reason.
    """
    outcome = delete_and_refresh_cache(
        paths, current_target, ask_permanent=_confirm_permanent_delete
    )
    return _delete_notice(outcome), bool(outcome)


# Suffixes that make a file something to be unpacked rather than opened.
ARCHIVE_SUFFIXES = frozenset(
    {".zip", ".tar", ".gz", ".xz", ".bz2", ".7z", ".rar", ".deb", ".rpm", ".apk"}
)


def _open_file_notice(path: Path) -> str:
    """Open one file, or reveal it in its folder when opening it would run it.

    Archives go to the file manager because unpacking is what the desktop would
    do with them and that is not what a row on this screen is asking for; the
    other two arms are the safety ones -- handing an executable, or a .desktop
    entry that can launch arbitrary actions, to xdg-open is a launch.
    """
    is_archive = path.suffix.lower() in ARCHIVE_SUFFIXES
    is_exec = os.access(path, os.X_OK)
    is_launchable = path.suffix.lower() == ".desktop"
    if is_archive or is_exec or is_launchable:
        return _open_in_file_manager(path)
    return _open_path(path, timeout=10)


def run_deep_analysis(target_path: Path | None = None):
    """Browse disk usage from one directory, drilling in and back out again.

    One turn of the loop is one frame: rebuild the rows if something invalidated
    them, draw them, read one key, act on it. Everything a frame is *about* lives
    in ``view`` and moves as a unit; what stays out of it is what the next turn
    has to do -- rescan or not, under which label, and the one notice the last
    keypress earned.

    Drilling in pushes a copy of the view and edits the original, so every way
    back out is the same pop: the BACK key, and a child directory that turned out
    to be unreadable.
    """
    state_stack: list[_View] = []
    view = _View(target=normalize_scan_path(target_path) if target_path else None)
    # What the next turn of the loop has to do, which is the part that is not the
    # view: whether the rows have to be rebuilt, what to call that while it runs,
    # and the one notice this frame owes the user for the last keypress.
    needs_scan = True
    scan_reason = "scan"
    action_notice = ""

    while True:
        target_to_scan = view.target or normalize_scan_path(Path.home())
        view_title = " Analyze Disk" if view.target is None else f" Exploring: {view.target}"

        if needs_scan:
            target_label = target_to_scan.name if view.target else "Home"
            view.data = _acquire_view_data(
                view.target, target_to_scan, scan_reason, target_label, view_title
            )
            if not view.data:
                # A bare print here was overwritten by the next frame, so the
                # 1.5 s sleep was the only thing that made it readable at all --
                # and when there was nowhere to go back to, the screen closed
                # with no explanation. Going back carries the reason in the
                # notice; leaving waits for a keypress instead of a timer.
                action_notice = _fail_notice(ENGINE_SCAN_FAILED_NOTICE)
                if not state_stack:
                    print(f"\n   {action_notice}")
                    Navigator.wait_for_return()
                    break
                view = state_stack.pop()
                needs_scan = False
                continue

            view.total_size = view.data.get("total_size_bytes", 0)
            if view.target is None:
                view.results, view.total_size = _root_view_results(
                    view.data, target_to_scan, view.total_size, view_title
                )
            else:
                view.results = _child_view_results(view.target, view.data, view.total_size)
            needs_scan = False
            scan_reason = "scan"

        selector = AnalyzeSelector(
            view_title,
            view.results,
            can_select=(view.target is not None),
            notice=action_notice or _explore_notice(view.data),
            sort_mode="name" if view.data and view.data.get("is_fast_explore") else "size",
        )
        # A notice raised by the last keypress is shown once and then drops away,
        # so it cannot outlive the action that caused it.
        action_notice = ""
        # Restore the cursor/page belonging to this view.  Parent views keep
        # these values on the navigation stack while a child directory is open.
        selector.selected_index = min(view.selected_index, max(0, len(view.results) - 1))
        selector.current_page = view.current_page
        action, idx = selector.run()
        if isinstance(selector.selected_index, int):
            view.selected_index = selector.selected_index
        if isinstance(selector.current_page, int):
            view.current_page = selector.current_page

        if action == "QUIT":
            break
        elif action == "BACK":
            if not state_stack:
                break
            view = state_stack.pop()
            # Recalculate parent percentages to reflect any deletions done in child
            for r in view.results:
                # Each row keeps the total it was measured against: in the
                # root view that is the filesystem the row lives on, which is
                # not the parent total.
                base = r.get("percent_base") or view.total_size
                if base > 0:
                    r["percent"] = percent_of(r["size"], base)
            needs_scan = False
        elif action == "REFRESH":
            ScanCache.discard(target_to_scan)
            needs_scan = True
            scan_reason = "refresh"
        elif action == "OPEN":
            path = view.results[idx]["path"]
            action_notice = _open_in_file_manager(path)
        elif action == "DRILL_DOWN":
            item = view.results[idx]
            if item.get("is_smart"):
                # For smart views, show a file list of the filtered items
                top_selector = TopFilesSelector(f"Smart View: {item['name']}", item["smart_items"])
                selected_idxs = top_selector.run()
                if selected_idxs:
                    paths = [item["smart_items"][s_idx]["path"] for s_idx in selected_idxs]
                    action_notice, deleted = _delete_selection(paths, view.target)
                    if deleted:
                        needs_scan = True
                        scan_reason = "refresh"
            elif item["path"].is_dir():
                # Safety: Avoid entering / as it's too heavy and requires sudo for full scan
                if str(item["path"]) == "/":
                    continue

                state_stack.append(view.snapshot())
                view.target = item["path"]
                view.selected_index = 0
                view.current_page = 0
                needs_scan = True
                scan_reason = "scan"
            elif item["path"].is_file():
                action_notice = _open_file_notice(item["path"])
        elif action == "DELETE_BATCH":
            # idx carries the ticked rows for a batch action, not one row.
            paths = [view.results[s_idx]["path"] for s_idx in idx]
            action_notice, deleted = _delete_selection(paths, view.target)
            if deleted:
                needs_scan = True
                scan_reason = "refresh"
        elif action == "OPEN_BATCH":
            for s_idx in idx:
                p = view.results[s_idx]["path"]
                action_notice = _open_in_file_manager(p) or action_notice
