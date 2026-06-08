import json
import os
import platform
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..ui.navigator import AnalyzeSelector, Navigator, TopFilesSelector
from . import system
from .app_cache import find_cleanable_cache_dirs, get_cache_cleanable_reason
from .constants import BLUE, CYAN, GREEN, MAGENTA, PURPLE, RED, RESET, YELLOW
from .file_ops import (
    get_size,
    record_deletion_audit,
    safe_remove,
    validate_path_for_deletion,
)
from .system import run_command

SCAN_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

# Grace period before a scan paints the (screen-clearing) scan header + spinner.
# Scans that finish within this window redraw in place like a cache hit, so fast
# small-directory scans don't flash/jitter; only slower scans show the spinner.
SCAN_SPINNER_DELAY = 0.15


# --- Internal Cache System ---
class ScanCache:
    """Memory-only cache for rust-engine scan results to make back navigation instant."""

    _data: dict[str, Any] = {}

    @classmethod
    def get(cls, path: Path) -> dict[str, Any] | None:
        return cls._data.get(str(path))

    @classmethod
    def set(cls, path: Path, data: dict[str, Any]):
        cls._data[str(path)] = data

    @classmethod
    def clear(cls):
        cls._data = {}


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


def get_rust_scan_data(path: Path) -> dict[str, Any] | None:
    """Calls the architecture-specific topo-core binary and returns parsed JSON."""
    binary = _get_core_binary()
    if binary is None:
        return None

    # Check cache first
    cached = ScanCache.get(path)
    if cached:
        return cached

    res = run_command([str(binary), str(path)], capture=True, timeout=300)
    if res.ok:
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError:
            return None
        ScanCache.set(path, data)
        return data
    return None


def get_rust_tree_data(path: Path) -> dict[str, Any] | None:
    """Scan the whole subtree under ``path`` in one pass.

    Returns a map ``{relative-dir-path: {total_size_bytes, file_count, subdirs}}``
    where ``"."`` is the root, used to prime the cache for every directory level
    so that drilling into any descendant needs no rescan. Returns None if the
    engine is missing, fails, or predates ``--tree`` support.
    """
    binary = _get_core_binary()
    if binary is None:
        return None

    res = run_command([str(binary), "--tree", str(path)], capture=True, timeout=600)
    if not res.ok:
        return None
    try:
        return json.loads(res.stdout)
    except json.JSONDecodeError:
        return None


def _prime_cache_from_tree(root: Path, tree: dict[str, Any]) -> None:
    """Populate ScanCache for every directory level returned by a tree scan.

    Keys are rejoined onto the original ``root`` (not the engine's canonicalized
    path) so they match how the UI builds child paths via ``parent / name`` —
    this is what keeps the cache hits working when ``root`` is a symlink.
    """
    for rel, node in tree.items():
        key = root if rel == "." else root.joinpath(*rel.split("/"))
        ScanCache.set(key, node)


def _parallel_scan_sizes(paths: list[Path]) -> dict[Path, int]:
    """Scan multiple paths concurrently via the Rust engine.

    Returns {path: total_size_bytes}. The work is subprocess/IO bound, so threads
    give a near-linear speedup over scanning the root categories serially.
    """
    sizes: dict[Path, int] = {}
    if not paths:
        return sizes

    def scan_one(p: Path) -> tuple[Path, int]:
        data = get_rust_scan_data(p)
        return p, (data.get("total_size_bytes", 0) if data else 0)

    with ThreadPoolExecutor(max_workers=min(8, len(paths))) as executor:
        for p, size in executor.map(scan_one, paths):
            sizes[p] = size
    return sizes


def _scan_status_message(scan_reason: str, target_label: str, frame: str) -> str:
    if scan_reason == "refresh":
        return f"   {frame} Refreshing analysis on {target_label}..."
    return f"   {frame} Rust Engine: Analyzing disk usage, please wait . . ."


def _render_scan_header(view_title: str) -> None:
    # Place the title exactly where AnalyzeSelector.render() puts it (home,
    # one blank line, then the title on row 2) so the screen does not shift
    # vertically when the scan screen hands off to the result list.
    print(f"\033[H\033[J\n{PURPLE}{view_title}{RESET}\033[K", flush=True)


def _scan_with_spinner(
    worker, scan_reason: str, target_label: str, view_title: str
) -> dict[str, Any] | None:
    """Run ``worker()`` in a background thread.

    If it finishes within ``SCAN_SPINNER_DELAY`` the scan screen is never
    painted, so fast scans (small dirs / mostly-cached subtrees) hand off to the
    result list with an in-place redraw — exactly like a cache hit, no flash or
    jitter. Only scans slower than the grace period clear the screen and animate
    the spinner."""
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
                        SCAN_SPINNER_FRAMES[frame_index % len(SCAN_SPINNER_FRAMES)],
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


def _tree_scan_with_spinner(
    path: Path, scan_reason: str, target_label: str, view_title: str
) -> dict[str, Any] | None:
    return _scan_with_spinner(
        lambda: get_rust_tree_data(path), scan_reason, target_label, view_title
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
    icon = "🧹" if is_cleanable else "🗂️" if path.is_dir() else "📄"
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
                            "size": get_size(item),
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
    if not valid:
        record_deletion_audit(target_path, "sudo-permanent", "rejected-validation")
        print(f" {RED}✗{RESET} {target_path}: {reason}")
        return False

    if not target_path.exists() and not target_path.is_symlink():
        record_deletion_audit(target_path, "sudo-permanent", "missing", 0)
        return False

    size_bytes = get_size(target_path)
    res = run_command(
        ["rm", "-rf", "--", str(target_path)], use_sudo=True, capture=True, timeout=300
    )
    if res.ok:
        record_deletion_audit(target_path, "sudo-permanent", "deleted", size_bytes)
        return True

    record_deletion_audit(target_path, "sudo-permanent", "failed", size_bytes)
    return False


def _safe_remove_analyze_path(path: Path) -> bool:
    removed, reason = safe_remove(path, use_trash=True)
    if removed:
        return True

    cleaned_child = False
    if reason == "Path is whitelisted":
        for child in find_cleanable_cache_dirs(path, require_sensitive_app_data_root=True):
            child_removed, child_reason = safe_remove(child, use_trash=True)
            if child_removed:
                cleaned_child = True
            else:
                print(f" {YELLOW}⚠{RESET} Skipped {child}: {child_reason}")

    if cleaned_child:
        return True

    print(f" {YELLOW}⚠{RESET} Skipped {Path(path).expanduser()}: {reason}")
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

    print(f" {GREEN}✓{RESET} Authorization successful.\n")
    return True


def _delete_analyze_paths(paths: list[Path]) -> bool:
    """Delete Analyze targets, using sudo only for paths outside user control."""
    if not _ensure_admin_for_delete(paths):
        return False

    admin_paths = [p for p in paths if _needs_admin_for_deletion(p)]
    changed = False
    for p in paths:
        removed = _sudo_remove(p) if p in admin_paths else _safe_remove_analyze_path(p)
        if removed:
            changed = True
    if changed:
        Navigator.play_delete()
    return changed


def run_deep_analysis(target_path: Path = None):
    # State Stack stores: {"target": Path, "results": [], "data": {}, "total_size": int}
    state_stack = []

    # Current active state
    current_target = target_path
    results = []
    data = None
    total_scan_size = 0
    needs_scan = True
    scan_reason = "scan"

    while True:
        target_to_scan = current_target or Path.home()
        view_title = "Analyze Disk" if current_target is None else f"Exploring: {current_target}"

        if needs_scan:
            target_label = target_to_scan.name if current_target else "Home"
            cached = ScanCache.get(target_to_scan)
            if cached is not None:
                # Cache hit (possibly primed by an earlier whole-subtree scan):
                # load instantly without painting the scan screen, so the view
                # doesn't blank/flash and shift vertically on every page turn.
                data = cached
            elif current_target is not None:
                # Exploring inside a directory: scan the WHOLE subtree once and
                # prime the cache for every level, so subsequent drilling never
                # rescans. The scan screen only appears if the scan is slow (see
                # _scan_with_spinner); fast small-dir scans redraw in place.
                # Falls back to a single-level scan for engines predating --tree.
                tree = _tree_scan_with_spinner(
                    target_to_scan, scan_reason, target_label, view_title
                )
                if tree:
                    _prime_cache_from_tree(target_to_scan, tree)
                    data = ScanCache.get(target_to_scan)
                else:
                    data = _get_rust_scan_data_with_spinner(
                        target_to_scan, scan_reason, target_label, view_title
                    )
            else:
                # Root view (categories): a single-level scan is all it needs.
                data = _get_rust_scan_data_with_spinner(
                    target_to_scan, scan_reason, target_label, view_title
                )
            if not data:
                print("\n   ❌ Engine scan failed.")
                time.sleep(1.5)
                if state_stack:
                    prev = state_stack.pop()
                    current_target = prev["target"]
                    results = prev["results"]
                    data = prev["data"]
                    total_scan_size = prev["total_size"]
                    needs_scan = False
                    continue
                else:
                    break

            total_scan_size = data.get("total_size_bytes", 0)
            results = []

            if current_target is None:
                # Root View: Standard Categories
                total_used = shutil.disk_usage("/").used or 1
                targets = [
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
                insights = [
                    {"name": "Old Downloads (90d+)", "path": home / "Downloads", "is_smart": True},
                    {"name": "Docker Data", "path": home / ".docker"},
                    {"name": "Docker System", "path": Path("/var/lib/docker")},
                    {"name": "Apt Cache", "path": Path("/var/cache/apt/archives")},
                    {"name": "Pacman Cache", "path": Path("/var/cache/pacman/pkg")},
                    {"name": "Dnf Cache", "path": Path("/var/cache/dnf")},
                    {"name": "Flatpak Data", "path": home / ".local/share/flatpak"},
                    {"name": "Ollama Models", "path": home / ".ollama" / "models"},
                ]

                # Collect every path that needs a Rust scan and run them concurrently.
                # Home is already scanned (total_scan_size); smart views use a Python
                # age-filter instead of a full scan.
                print(
                    "\r   • Rust Engine: Analyzing Linux insights, please wait . . .\033[K",
                    end="",
                    flush=True,
                )
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
                scan_sizes = _parallel_scan_sizes(rust_paths)

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

                        if size > 10 * 1024 * 1024:  # Only show if > 10MB to keep Root clean
                            results.append(
                                {
                                    "name": ins["name"],
                                    "path": p,
                                    "size": size,
                                    "percent": (size / total_used) * 100,
                                    "color": YELLOW,
                                    "icon": "👀",
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
                for name, size in subdir_map.items():
                    full_path = current_target / name
                    results.append(build_analysis_entry(name, full_path, size, total_path_size))
                results.sort(key=lambda x: x["size"], reverse=True)
                results = results[:50]
            needs_scan = False
            scan_reason = "scan"

        selector = AnalyzeSelector(
            view_title,
            results,
            can_select=(current_target is not None),
        )
        action, idx = selector.run()

        if action == "QUIT":
            break
        elif action == "BACK":
            if state_stack:
                prev = state_stack.pop()
                current_target = prev["target"]
                results = prev["results"]
                data = prev["data"]
                total_scan_size = prev["total_size"]
                # Recalculate parent percentages to reflect any deletions done in child
                if total_scan_size > 0:
                    for r in results:
                        r["percent"] = (r["size"] / total_scan_size) * 100
                needs_scan = False
            else:
                break
        elif action == "REFRESH":
            ScanCache._data.pop(str(target_to_scan), None)
            needs_scan = True
            scan_reason = "refresh"
        elif action == "OPEN":
            path = results[idx]["path"]
            parent = path.parent if path.exists() else path
            run_command(["xdg-open", str(parent)], capture=True, timeout=10)
        elif action == "DRILL_DOWN":
            item = results[idx]
            if item.get("is_smart"):
                # For smart views, show a file list of the filtered items
                top_selector = TopFilesSelector(f"Smart View: {item['name']}", item["smart_items"])
                selected_idxs = top_selector.run()
                if selected_idxs:
                    paths = [item["smart_items"][s_idx]["path"] for s_idx in selected_idxs]
                    if _delete_analyze_paths(paths):
                        ScanCache.clear()
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
                    }
                )
                current_target = item["path"]
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
                    run_command(["xdg-open", str(p.parent)], capture=True, timeout=10)
                else:
                    run_command(["xdg-open", str(p)], capture=True, timeout=10)
        elif action == "DELETE_BATCH":
            selected_idxs = idx  # action was DELETE_BATCH, idx contains the list
            paths = [results[s_idx]["path"] for s_idx in selected_idxs]
            if _delete_analyze_paths(paths):
                ScanCache.clear()
                needs_scan = True
                scan_reason = "refresh"
        elif action == "OPEN_BATCH":
            selected_idxs = idx
            for s_idx in selected_idxs:
                p = results[s_idx]["path"]
                parent = p.parent if p.exists() else p
                run_command(["xdg-open", str(parent)], capture=True, timeout=10)
