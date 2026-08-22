import functools
import json
import os
import platform
import shutil
import stat
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..ui.navigator import AnalyzeSelector, ConfirmSelector, TopFilesSelector
from . import system
from .app_cache import find_cleanable_cache_dirs, get_cache_cleanable_reason
from .constants import (
    BLUE,
    CYAN,
    ERASE_BELOW,
    GRAY,
    GREEN,
    MAGENTA,
    PURPLE,
    RED,
    RESET,
    YELLOW,
)
from .file_ops import (
    TRASH_UNAVAILABLE_REASON,
    get_size_fast,
    record_deletion_audit,
    safe_remove,
    validate_path_for_deletion,
)
from .heavy_cache import get_analyze_cache_defs
from .scan_cache import ScanCache
from .sound import play_delete
from .spinner import DEFAULT_SPINNER_FRAMES
from .system import run_command
from .text import sanitize_for_display

_ANALYZE_COMMAND_TIMEOUT = 300

# Grace period before a scan paints the scan header + spinner. Scans that
# finish within this window redraw in place like a cache hit, so fast
# small-directory scans don't flash/jitter; only slower scans show the spinner.
SCAN_SPINNER_DELAY = 0.15
ANALYZE_RESULT_LIMIT = 50
FAST_EXPLORE_ENTRY_LIMIT = 500


@functools.cache
def _get_core_binary() -> Path | None:
    """Resolves the architecture-specific topo-core binary path.

    install.sh keeps only the binary matching the host arch (e.g. it removes
    topo-core-x86_64 on ARM64), so we must pick the name dynamically. Falls back
    to any available engine binary for dev/single-arch checkouts.
    """
    bin_dir = Path(__file__).parent / "bin"
    arch = platform.machine().lower()
    suffix = "aarch64" if arch in ("aarch64", "arm64") else "x86_64"
    preferred = bin_dir / f"topo-core-{suffix}"
    if preferred.exists():
        return preferred
    for candidate in sorted(bin_dir.glob("topo-core-*")):
        if candidate.is_file():
            return candidate
    return None


def _normalize_scan_path(path: str | Path) -> Path:
    """Return one stable absolute cache/process key without leaking resolve errors."""
    raw = Path(path).expanduser()
    try:
        return raw.resolve(strict=False)
    except (OSError, RuntimeError):
        return raw.absolute()


def get_rust_scan_data(path: Path, *, use_cache: bool = True) -> dict[str, Any] | None:
    """Calls the architecture-specific topo-core binary and returns parsed JSON."""
    binary = _get_core_binary()
    if binary is None:
        return None

    path = _normalize_scan_path(path)
    # Check cache first
    cached = ScanCache.get(path)
    if use_cache and cached:
        return cached

    res = run_command([str(binary), str(path)], capture=True, timeout=_ANALYZE_COMMAND_TIMEOUT)
    if res.ok:
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError:
            return None
        ScanCache.set(path, data)
        return data
    return None


def get_rust_tree_data(path: Path) -> dict[str, Any] | None:
    """Scan once and seed ScanCache for every significant descendant."""
    binary = _get_core_binary()
    if binary is None:
        return None

    path = _normalize_scan_path(path)
    res = run_command(
        [str(binary), "--tree", str(path)],
        capture=True,
        timeout=_ANALYZE_COMMAND_TIMEOUT,
    )
    if not res.ok:
        return None
    try:
        tree = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(tree, dict) or not isinstance(tree.get("."), dict):
        return None
    root_data = None
    for relative, aggregate in tree.items():
        if not isinstance(aggregate, dict):
            continue
        node = path if relative == "." else path / relative
        data_item = {"path": str(node), "top_files": [], **aggregate}
        if relative == ".":
            root_data = data_item
            continue
        ScanCache.set(node, data_item)
    if root_data:
        ScanCache.set(path, root_data)
    return root_data or ScanCache.get(path)


def _direct_child_count_exceeds(path: Path, limit: int = FAST_EXPLORE_ENTRY_LIMIT) -> bool:
    try:
        with os.scandir(path) as entries:
            for count, _entry in enumerate(entries, 1):
                if count > limit:
                    return True
    except OSError:
        return False
    return False


def _should_use_fast_explore(
    path: Path, direct_entry_limit: int = FAST_EXPLORE_ENTRY_LIMIT
) -> bool:
    return _direct_child_count_exceeds(path, direct_entry_limit)


def get_fast_explore_data(
    path: Path, entry_limit: int = FAST_EXPLORE_ENTRY_LIMIT
) -> dict[str, Any] | None:
    """Build a bounded direct-child listing without recursively scanning.

    Used only for very wide directories where calculating every direct child
    size would make opening the view feel stuck.
    """
    subdirs: dict[str, int] = {}
    entry_meta: dict[str, dict[str, bool]] = {}
    total_size = 0
    file_count = 0
    sampled_entries = 0
    truncated = False
    try:
        with os.scandir(path) as entries:
            for count, entry in enumerate(entries, 1):
                if count > entry_limit:
                    truncated = True
                    break
                sampled_entries = count
                try:
                    is_dir = entry.is_dir(follow_symlinks=False)
                    if is_dir:
                        size = 0
                        size_known = False
                    else:
                        stat_result = entry.stat(follow_symlinks=False)
                        size = stat_result.st_size
                        size_known = True
                        file_count += 1
                except OSError:
                    continue

                subdirs[entry.name] = size
                entry_meta[entry.name] = {
                    "is_dir": is_dir,
                    "size_known": size_known,
                }
                if size_known:
                    total_size += size
    except OSError:
        return None

    data = {
        "path": str(path),
        "total_size_bytes": total_size,
        "file_count": file_count,
        "subdirs": subdirs,
        "entry_meta": entry_meta,
        "top_files": [],
        "is_fast_explore": True,
        "preview_entry_limit": entry_limit,
        "preview_sampled_entries": sampled_entries,
        "preview_truncated": truncated,
    }
    return data


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


def _parallel_scan_sizes(
    paths: list[Path], *, on_scan_start: Callable[[], None] | None = None
) -> dict[Path, int]:
    """Scan multiple paths concurrently via the Rust engine.

    Returns {path: total_size_bytes}. The work is subprocess/IO bound, so threads
    give a near-linear speedup over scanning the root categories serially.
    """
    sizes: dict[Path, int] = {}
    if not paths:
        return sizes

    scan_started = False

    def notify_scan_start() -> None:
        nonlocal scan_started
        if not scan_started and on_scan_start is not None:
            on_scan_start()
        scan_started = True

    unique = list(dict.fromkeys(paths))
    norm = {p: _normalize_scan_path(p) for p in unique}
    roots = [p for p in unique if not any(parent in unique for parent in p.parents)]
    roots = [p for p in roots if ScanCache.get(norm[p]) is None]

    def scan_one(p: Path) -> None:
        get_rust_tree_data(p)

    if roots:
        notify_scan_start()
        with ThreadPoolExecutor(max_workers=min(2, len(roots))) as executor:
            list(executor.map(scan_one, roots))
    missing = [path for path in unique if ScanCache.get(norm[path]) is None]
    if missing:
        notify_scan_start()
        with ThreadPoolExecutor(max_workers=min(2, len(missing))) as executor:
            list(executor.map(get_rust_scan_data, missing))
    for path in unique:
        data = ScanCache.get(norm[path])
        sizes[path] = data.get("total_size_bytes", 0) if data else 0
    return sizes


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


def get_age_hint(path: Path) -> str:
    """Returns a rough age hint like >90d, >6mo, >1y based on mtime."""
    try:
        mtime = path.stat().st_mtime
        days = (time.time() - mtime) / 86400
        if days < 30:
            return ""
        if days > 365:
            return f">{int(days / 365)}y"
        if days > 30:
            return f">{int(days / 30)}mo"
        return f">{int(days)}d"
    except OSError:
        return ""


def build_analysis_entry(name: str, path: Path, size: int, total_size: int) -> dict[str, Any]:
    """Build a disk-analysis row with Linux cache metadata."""
    cleanable_reason = get_cache_cleanable_reason(path)
    is_cleanable = bool(cleanable_reason)
    icon = "🗂️" if path.is_dir() else "📄"
    return {
        "name": name,
        "path": path,
        "size": size,
        "percent": (size / (total_size or 1)) * 100,
        "icon": icon,
        "is_cleanable": is_cleanable,
        "cleanable_reason": cleanable_reason,
        "age_hint": get_age_hint(path),
    }


def build_linux_insights(home: Path) -> list[dict[str, Any]]:
    insights: list[dict[str, Any]] = [
        {
            "name": "Old Downloads (90d+)",
            "path": home / "Downloads",
            "is_smart": True,
            "icon": "🕒",
        }
    ]
    insights.extend(
        {
            "name": definition.label,
            "path": definition.resolved_path(),
            "min_display_bytes": definition.min_display_bytes,
            "icon": definition.icon,
        }
        for definition in get_analyze_cache_defs()
    )
    return insights


def get_old_items_info(dir_path: Path, days_threshold: int = 90) -> list[dict[str, Any]]:
    """Returns a list of items in a directory older than X days."""
    old_items = []
    cutoff = time.time() - (days_threshold * 86400)
    try:
        for item in dir_path.iterdir():
            try:
                stat = item.stat()
                if stat.st_mtime < cutoff:
                    old_items.append(
                        {
                            "name": item.name,
                            "path": item,
                            "size": get_size_fast(item),
                            "mtime": stat.st_mtime,
                        }
                    )
            except OSError:
                continue
    except OSError:
        pass
    return sorted(old_items, key=lambda x: x["size"], reverse=True)


def _needs_admin_for_deletion(path: Path) -> bool:
    """Return True when a deletion target should go through sudo."""
    raw_path = Path(path).expanduser()
    try:
        resolved_path = raw_path.resolve(strict=False)
    except OSError:
        resolved_path = raw_path.absolute()

    home = Path.home().resolve()
    try:
        is_in_home = resolved_path == home or home in resolved_path.parents
    except RuntimeError:
        is_in_home = False
    if not is_in_home:
        return True

    try:
        stat = raw_path.lstat()
    except OSError:
        return True

    parent = raw_path.parent
    return stat.st_uid != os.getuid() or not os.access(parent, os.W_OK | os.X_OK)


def _sudo_remove(path: Path) -> bool:
    """Remove a validated Analyze target with sudo and record an audit event."""
    raw_path = Path(path).expanduser()
    # Resolve once and operate on that exact path for the rest of the function.
    # Validation, the existence check, the size read and `rm -rf` must all act on
    # the SAME byte-for-byte path — otherwise validation could clear the
    # symlink-resolved target while `rm` (run as root) acts on the raw string,
    # i.e. validate path A but delete path B.
    try:
        target_path = raw_path.resolve(strict=False)
    except OSError:
        target_path = raw_path.absolute()

    valid, reason = validate_path_for_deletion(target_path)
    # These messages report a *security decision* about an attacker-controllable
    # name, so the name must never be able to rewrite the line it is printed on.
    safe_target = sanitize_for_display(str(target_path))
    if not valid:
        record_deletion_audit(target_path, "sudo-permanent", "rejected-validation")
        print(f" {RED}✗{RESET} {safe_target}: {reason}")
        return False

    if not target_path.exists() and not target_path.is_symlink():
        record_deletion_audit(target_path, "sudo-permanent", "missing", 0)
        return False

    size_bytes = get_size_fast(target_path)
    current_uid = os.getuid()
    ancestors = list(target_path.parents)
    for index, parent in enumerate(ancestors):
        safe_parent = sanitize_for_display(str(parent))
        try:
            st = parent.lstat()
        except OSError:
            record_deletion_audit(target_path, "sudo-permanent", "rejected-unreadable-ancestor")
            print(f" {RED}✗{RESET} {safe_target}: cannot stat path component ({safe_parent})")
            return False

        if stat.S_ISLNK(st.st_mode):
            record_deletion_audit(target_path, "sudo-permanent", "rejected-ancestor-symlink")
            print(f" {RED}✗{RESET} {safe_target}: ancestor directory is a symlink ({safe_parent})")
            return False

        # `rm` receives a pathname, so every directory used to resolve that name
        # must be immune to replacement by the invoking user until sudo opens it.
        # A sticky shared directory is safe only for the final name directly
        # below it: sticky semantics protect that entry, but not a user-owned
        # directory nested below it.
        user_can_replace = st.st_uid == current_uid and bool(st.st_mode & stat.S_IWUSR)
        shared_writable = bool(st.st_mode & 0o022)
        direct_sticky_parent = index == 0 and bool(st.st_mode & stat.S_ISVTX)
        foreign_owner = st.st_uid not in (0, current_uid) and not direct_sticky_parent
        if user_can_replace or (shared_writable and not direct_sticky_parent) or foreign_owner:
            record_deletion_audit(target_path, "sudo-permanent", "rejected-unsafe-ancestor")
            print(f" {RED}✗{RESET} {safe_target}: untrusted ancestor directory ({safe_parent})")
            return False

    res = run_command(
        ["rm", "-rf", "--one-file-system", "--", str(target_path)],
        use_sudo=True,
        capture=True,
        timeout=_ANALYZE_COMMAND_TIMEOUT,
    )
    if res.ok:
        record_deletion_audit(target_path, "sudo-permanent", "deleted", size_bytes)
        return True

    record_deletion_audit(target_path, "sudo-permanent", "failed", size_bytes)
    return False


def _permanent_fallback_consent() -> Callable[[Path], bool]:
    """Build a one-shot gate for turning a recoverable delete into a permanent one.

    Choosing "delete" in Analyze consents to a *recoverable* delete. On a system
    with no trash backend (containers, servers, minimal WMs) that promise cannot
    be kept, so the downgrade needs its own answer instead of being substituted
    silently. The question is asked at most once per batch, and anything
    non-interactive answers "no" — skipping is always the recoverable choice.
    """
    granted: bool | None = None

    def consent(path: Path) -> bool:
        nonlocal granted
        if granted is not None:
            return granted
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            granted = False
            print(
                f" {YELLOW}⚠{RESET} No trash backend available and no terminal to ask; "
                "skipping instead of deleting permanently."
            )
            return granted
        granted = ConfirmSelector(
            "No trash backend (gio / trash-put) found. Delete permanently? This cannot be undone."
        ).run()
        return granted

    return consent


def _safe_remove_analyze_path(
    path: Path,
    permanent_consent: Callable[[Path], bool] | None = None,
) -> bool:
    removed, reason = safe_remove(path, use_trash=True)
    if removed:
        return True

    # Trash unavailable: permanent removal happens only with the user's explicit
    # consent for this batch, never as a library-level substitution.
    if (
        reason == TRASH_UNAVAILABLE_REASON
        and permanent_consent is not None
        and permanent_consent(path)
    ):
        removed, reason = safe_remove(path, use_trash=False)
        if removed:
            return True

    cleaned_child = False
    if reason == "Path is whitelisted":
        for child in find_cleanable_cache_dirs(path, require_sensitive_app_data_root=True):
            child_removed, child_reason = safe_remove(child, use_trash=True)
            if (
                not child_removed
                and child_reason == TRASH_UNAVAILABLE_REASON
                and permanent_consent is not None
                and permanent_consent(child)
            ):
                child_removed, child_reason = safe_remove(child, use_trash=False)
            if child_removed:
                cleaned_child = True
            else:
                safe_child = sanitize_for_display(str(child))
                print(f" {YELLOW}⚠{RESET} Skipped {safe_child}: {child_reason}")

    if cleaned_child:
        return True

    safe_path = sanitize_for_display(str(Path(path).expanduser()))
    print(f" {YELLOW}⚠{RESET} Skipped {safe_path}: {reason}")
    return False


def _ensure_admin_for_delete(paths: list[Path]) -> bool:
    """Prompts for sudo only if any path in the list requires admin privileges."""
    admin_paths = [p for p in paths if _needs_admin_for_deletion(p)]
    if not admin_paths:
        return True

    print()
    if not system.ensure_sudo_session(
        f"{MAGENTA}➔{RESET} File deletion requires admin access\n{MAGENTA}➔{RESET} Password: "
    ):
        if system.SUDO_CANCELLED:
            print(f" {YELLOW}⚠️  Delete cancelled by user.{RESET}\n")
        else:
            print(f" {RED}✗{RESET} Authorization failed. Delete cancelled.\n")
        return False

    print(f"{GREEN}ꗃ{RESET} Authorization successful.\n")
    return True


def _delete_analyze_paths(paths: list[Path]) -> bool:
    """Delete Analyze targets, using sudo only for paths outside user control."""
    if not _ensure_admin_for_delete(paths):
        return False

    admin_paths = [p for p in paths if _needs_admin_for_deletion(p)]
    changed = False
    consent = _permanent_fallback_consent()
    for p in paths:
        removed = (
            _sudo_remove(p)
            if p in admin_paths
            else _safe_remove_analyze_path(p, permanent_consent=consent)
        )
        if removed:
            changed = True
    if changed:
        play_delete()
    return changed


def _open_in_file_manager(path: Path) -> None:
    """Open the parent directory of *path* in the system file manager."""
    target = path.parent if path.is_file() else path
    run_command(["xdg-open", str(target)], capture=True, timeout=5)


def _delete_and_refresh_cache(paths: list[Path], current_target: Path | None) -> bool:
    """Delete paths and invalidate scan caches on success."""
    if not _delete_analyze_paths(paths):
        return False
    if current_target:
        ScanCache.discard(current_target)
    else:
        ScanCache.clear()
    return True


def run_deep_analysis(target_path: Path | None = None):
    # State Stack stores: {"target": Path, "results": [], "data": {}, "total_size": int}
    state_stack: list[dict[str, Any]] = []

    # Current active state
    current_target: Path | None = _normalize_scan_path(target_path) if target_path else None
    results: list[dict[str, Any]] = []
    data: dict[str, Any] | None = None
    total_scan_size = 0
    needs_scan = True
    scan_reason = "scan"
    selected_index = 0
    current_page = 0

    while True:
        target_to_scan = current_target or _normalize_scan_path(Path.home())
        view_title = "Analyze Disk" if current_target is None else f"Exploring: {current_target}"

        if needs_scan:
            target_label = target_to_scan.name if current_target else "Home"
            if current_target is not None:
                if _should_use_fast_explore(target_to_scan):
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
                        lambda paths=rust_paths: _parallel_scan_sizes(paths),
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
                                "icon": "📊" if str(t["path"]) == "/" else "🗂️",
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
                            "icon": "🗂️" if is_dir else "📄",
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
                    if _delete_and_refresh_cache(paths, current_target):
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
            if _delete_and_refresh_cache(paths, current_target):
                needs_scan = True
                scan_reason = "refresh"
        elif action == "OPEN_BATCH":
            selected_idxs = idx
            for s_idx in selected_idxs:
                p = results[s_idx]["path"]
                _open_in_file_manager(p)
