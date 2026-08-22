import functools
import json
import os
import platform
import stat
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from . import system
from .app_cache import find_cleanable_cache_dirs, get_cache_cleanable_reason
from .constants import (
    GREEN,
    MAGENTA,
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
def get_core_binary() -> Path | None:
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


def normalize_scan_path(path: str | Path) -> Path:
    """Return one stable absolute cache/process key without leaking resolve errors."""
    raw = Path(path).expanduser()
    try:
        return raw.resolve(strict=False)
    except (OSError, RuntimeError):
        return raw.absolute()


def get_rust_scan_data(path: Path, *, use_cache: bool = True) -> dict[str, Any] | None:
    """Calls the architecture-specific topo-core binary and returns parsed JSON."""
    binary = get_core_binary()
    if binary is None:
        return None

    path = normalize_scan_path(path)
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
    binary = get_core_binary()
    if binary is None:
        return None

    path = normalize_scan_path(path)
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


def should_use_fast_explore(path: Path, direct_entry_limit: int = FAST_EXPLORE_ENTRY_LIMIT) -> bool:
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


def parallel_scan_sizes(
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
    norm = {p: normalize_scan_path(p) for p in unique}
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


PERMANENT_DELETE_QUESTION = (
    "No trash backend (gio / trash-put) found. Delete permanently? This cannot be undone."
)


def _permanent_fallback_consent(
    ask: Callable[[str], bool] | None = None,
) -> Callable[[Path], bool]:
    """Build a one-shot gate for turning a recoverable delete into a permanent one.

    Choosing "delete" in Analyze consents to a *recoverable* delete. On a system
    with no trash backend (containers, servers, minimal WMs) that promise cannot
    be kept, so the downgrade needs its own answer instead of being substituted
    silently. The question is asked at most once per batch, and anything
    non-interactive answers "no" — skipping is always the recoverable choice.

    *ask* poses the question and returns the answer; the caller supplies it
    because putting up a dialog is the UI layer's job, while deciding that the
    question is warranted, asking it once, and defaulting to "no" is this
    module's. A caller with no way to ask (``ask=None``) is treated exactly like
    a run with no terminal.
    """
    granted: bool | None = None

    def consent(path: Path) -> bool:
        nonlocal granted
        if granted is not None:
            return granted
        if ask is None or not (sys.stdin.isatty() and sys.stdout.isatty()):
            granted = False
            print(
                f" {YELLOW}⚠{RESET} No trash backend available and no terminal to ask; "
                "skipping instead of deleting permanently."
            )
            return granted
        granted = ask(PERMANENT_DELETE_QUESTION)
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


def _delete_analyze_paths(
    paths: list[Path], *, ask_permanent: Callable[[str], bool] | None = None
) -> bool:
    """Delete Analyze targets, using sudo only for paths outside user control."""
    if not _ensure_admin_for_delete(paths):
        return False

    admin_paths = [p for p in paths if _needs_admin_for_deletion(p)]
    changed = False
    consent = _permanent_fallback_consent(ask_permanent)
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


def delete_and_refresh_cache(
    paths: list[Path],
    current_target: Path | None,
    *,
    ask_permanent: Callable[[str], bool] | None = None,
) -> bool:
    """Delete paths and invalidate scan caches on success."""
    if not _delete_analyze_paths(paths, ask_permanent=ask_permanent):
        return False
    if current_target:
        ScanCache.discard(current_target)
    else:
        ScanCache.clear()
    return True
