import os
import shutil
import stat
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import system
from .core.app_cache import find_cleanable_cache_dirs, get_cache_cleanable_reason
from .core.config import get_use_trash
from .core.constants import (
    MAGENTA,
    MARK_PROMPT,
    RESET,
    SECONDS_PER_DAY,
    WARN,
)
from .core.engine import get_rust_scan_data, get_rust_tree_data, normalize_scan_path
from .core.file_ops import (
    TRASH_UNAVAILABLE_REASON,
    get_direct_child_sizes_fast,
    get_size_fast,
    record_deletion_audit,
    safe_remove,
    validate_path_for_deletion,
)
from .core.file_types import icon_for_entry
from .core.heavy_cache import get_analyze_cache_defs
from .core.scan_cache import ScanCache, ScanResult
from .core.sound import play_delete
from .core.system import run_command
from .core.text import sanitize_for_display

# Grace period before a scan paints the scan header + spinner. Scans that
# finish within this window redraw in place like a cache hit, so fast
# small-directory scans don't flash/jitter; only slower scans show the spinner.
# The rm -rf that a sudo delete falls back to. Shared a constant with the
# scanner's timeout while both lived here, though they time different things.
_SUDO_REMOVE_TIMEOUT = 300

SCAN_SPINNER_DELAY = 0.15
ANALYZE_RESULT_LIMIT = 50
FAST_EXPLORE_ENTRY_LIMIT = 500


class FastExploreResult(ScanResult, total=False):
    """What get_fast_explore_data returns: a listing rather than a measurement.

    The scan shape plus the five keys that say so, because the Analyze view draws
    either record with the same code -- ``is_fast_explore`` is the one the reading
    side asks, and the one ScanCache.set refuses. What the two do not share is
    depth: nothing here is recursive, so every folder size in ``subdirs`` is zero,
    ``top_files`` is empty, and ``entry_meta`` says per entry which of those zeros
    means "a directory nobody sized" rather than "an empty file".
    """

    entry_meta: dict[str, dict[str, bool]]
    is_fast_explore: bool
    preview_entry_limit: int
    preview_sampled_entries: int
    preview_truncated: bool


def get_fast_explore_data(
    path: Path, entry_limit: int = FAST_EXPLORE_ENTRY_LIMIT, *, only_when_wide: bool = False
) -> FastExploreResult | None:
    """Build a bounded direct-child listing without recursively scanning.

    Used only for very wide directories where calculating every direct child
    size would make opening the view feel stuck.

    With *only_when_wide* the result is ``None`` unless the directory really is
    wider than *entry_limit*, which lets the caller decide between preview mode
    and a full scan from this single pass. The names are collected first and
    stat'd afterwards, so a directory that turns out to be narrow costs one
    ``readdir`` sweep and no per-entry syscalls -- the deciding walk and the
    sampling walk used to be two separate traversals, i.e. two round trips on an
    NFS or sshfs mount.
    """
    subdirs: dict[str, int] = {}
    entry_meta: dict[str, dict[str, bool]] = {}
    total_size = 0
    file_count = 0
    truncated = False
    sampled: list[os.DirEntry[str]] = []
    try:
        with os.scandir(path) as entries:
            for count, entry in enumerate(entries, 1):
                if count > entry_limit:
                    truncated = True
                    break
                sampled.append(entry)
    except OSError:
        return None

    if only_when_wide and not truncated:
        return None

    for entry in sampled:
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

    data: FastExploreResult = {
        "path": str(path),
        "total_size_bytes": total_size,
        "file_count": file_count,
        "subdirs": subdirs,
        "entry_meta": entry_meta,
        "top_files": [],
        "is_fast_explore": True,
        "preview_entry_limit": entry_limit,
        "preview_sampled_entries": len(sampled),
        "preview_truncated": truncated,
    }
    return data


def filesystem_used_bytes(path: Path) -> int:
    """Used bytes of the filesystem that holds *path*.

    Root-view shares are measured against the disk the row actually lives on. A
    single ``/`` denominator is meaningless the moment /home is its own
    partition -- the layout Debian's installer offers by default -- where 500 GB
    of Home over a 20 GB root printed 2500%.
    """
    for candidate in (path, *path.parents):
        try:
            return shutil.disk_usage(candidate).used
        except OSError:
            continue
    return 0


def percent_of(size: int, total: int) -> float:
    """Share of *total* taken by *size*, capped at 100%.

    The cap matters where the two numbers come from different measurements: a
    scanned tree sums apparent file sizes while ``disk_usage`` reports allocated
    blocks, so hard links or sparse files can push the ratio just past 1.
    """
    return min((size / (total or 1)) * 100, 100.0)


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
        days = (time.time() - mtime) / SECONDS_PER_DAY
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
    icon = icon_for_entry(path, is_dir=path.is_dir())
    return {
        "name": name,
        "path": path,
        "size": size,
        "percent": percent_of(size, total_size),
        "percent_base": total_size,
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
    cutoff = time.time() - (days_threshold * SECONDS_PER_DAY)
    # One scan of the parent already holds every direct child's size, and the
    # root view has just walked Home, so this is normally a cache hit costing no
    # subprocess at all. Sizing each row on its own forked the engine once per
    # old entry, serially, and each of those scans also pushed a fresh entry into
    # the shared ScanCache -- evicting the Home tree it had just filled.
    child_sizes = get_direct_child_sizes_fast(dir_path)
    try:
        for item in dir_path.iterdir():
            try:
                stat_result = item.stat()
                if stat_result.st_mtime < cutoff:
                    is_dir = stat.S_ISDIR(stat_result.st_mode)
                    old_items.append(
                        {
                            "name": item.name,
                            "path": item,
                            "size": _old_item_size(item, stat_result, is_dir, child_sizes),
                            "mtime": stat_result.st_mtime,
                            # Taken from the stat already in hand: the row icon
                            # needs it, and probing again per render would be a
                            # syscall a keystroke.
                            "is_dir": is_dir,
                        }
                    )
            except OSError:
                continue
    except OSError:
        pass
    return sorted(old_items, key=lambda x: x["size"], reverse=True)


def _old_item_size(
    item: Path,
    stat_result: os.stat_result,
    is_dir: bool,
    child_sizes: dict[str, int] | None,
) -> int:
    """Size of one old-downloads row, preferring the parent's single scan.

    ``child_sizes`` is None only when no fast scan was available, which is the
    one case that still needs a per-item scan. A name missing from a successful
    scan is one the engine left out -- it held nothing, or it is a symlinked
    directory the walk refuses to follow -- and either way removing that row
    frees nothing here, so 0 is the answer without asking again. A file's size is
    already in the stat in hand.
    """
    if child_sizes is not None:
        cached_size = child_sizes.get(item.name)
        if cached_size is not None:
            return cached_size
        return 0 if is_dir else stat_result.st_size
    return get_size_fast(item)


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


def _sudo_remove(path: Path) -> tuple[bool, int, str]:
    """Remove a validated Analyze target with sudo and record an audit event.

    Returns (removed, freed_bytes, problem). *problem* is a plain, uncoloured
    sentence for the caller to render; nothing here prints, because Analyze
    repaints a whole frame the moment the batch returns and anything written
    straight to the terminal is overwritten inside the same tick.
    """
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
        return False, 0, f"{safe_target}: {reason}"

    if not target_path.exists() and not target_path.is_symlink():
        record_deletion_audit(target_path, "sudo-permanent", "missing", 0)
        return False, 0, f"{safe_target}: no longer there"

    size_bytes = get_size_fast(target_path)
    current_uid = os.getuid()
    ancestors = list(target_path.parents)
    for index, parent in enumerate(ancestors):
        safe_parent = sanitize_for_display(str(parent))
        try:
            st = parent.lstat()
        except OSError:
            record_deletion_audit(target_path, "sudo-permanent", "rejected-unreadable-ancestor")
            return False, 0, f"{safe_target}: cannot stat path component ({safe_parent})"

        if stat.S_ISLNK(st.st_mode):
            record_deletion_audit(target_path, "sudo-permanent", "rejected-ancestor-symlink")
            return False, 0, f"{safe_target}: ancestor directory is a symlink ({safe_parent})"

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
            return False, 0, f"{safe_target}: untrusted ancestor directory ({safe_parent})"

    res = run_command(
        ["rm", "-rf", "--one-file-system", "--", str(target_path)],
        use_sudo=True,
        capture=True,
        timeout=_SUDO_REMOVE_TIMEOUT,
    )
    if res.ok:
        record_deletion_audit(target_path, "sudo-permanent", "deleted", size_bytes)
        return True, size_bytes, ""

    record_deletion_audit(target_path, "sudo-permanent", "failed", size_bytes)
    return False, 0, f"{safe_target}: rm failed as root"


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
                f" {WARN} No trash backend available and no terminal to ask; "
                "skipping instead of deleting permanently."
            )
            return granted
        granted = ask(PERMANENT_DELETE_QUESTION)
        return granted

    return consent


def _safe_remove_analyze_path(
    path: Path,
    permanent_consent: Callable[[Path], bool] | None = None,
) -> tuple[bool, int, str]:
    """Remove one user-owned Analyze target: (removed, freed_bytes, problem).

    The size is read before the removal, because afterwards there is nothing left
    to measure; that walk is the same get_size_fast() the sudo path already pays.
    *problem* is plain uncoloured text -- see _sudo_remove for why nothing here
    prints its own line.
    """
    # config.json's use_trash is consent given in advance: with it turned off the
    # first attempt is already the permanent one, and the consent prompt below
    # never comes up because there is no trash step left to fail.
    use_trash = get_use_trash()
    size_bytes = get_size_fast(path)
    removed, reason = safe_remove(path, use_trash=use_trash)
    if removed:
        return True, size_bytes, ""

    # Trash unavailable: permanent removal happens only with the user's explicit
    # consent for this batch, never as a library-level substitution.
    if (
        reason == TRASH_UNAVAILABLE_REASON
        and permanent_consent is not None
        and permanent_consent(path)
    ):
        removed, reason = safe_remove(path, use_trash=False)
        if removed:
            return True, size_bytes, ""

    cleaned_child = False
    freed_bytes = 0
    first_problem = ""
    if reason == "Path is whitelisted":
        for child in find_cleanable_cache_dirs(path, require_sensitive_app_data_root=True):
            child_size = get_size_fast(child)
            child_removed, child_reason = safe_remove(child, use_trash=use_trash)
            if (
                not child_removed
                and child_reason == TRASH_UNAVAILABLE_REASON
                and permanent_consent is not None
                and permanent_consent(child)
            ):
                child_removed, child_reason = safe_remove(child, use_trash=False)
            if child_removed:
                cleaned_child = True
                freed_bytes += child_size
            elif not first_problem:
                safe_child = sanitize_for_display(str(child))
                first_problem = f"Skipped {safe_child}: {child_reason}"

    if cleaned_child:
        return True, freed_bytes, first_problem

    safe_path = sanitize_for_display(str(Path(path).expanduser()))
    return False, 0, f"Skipped {safe_path}: {reason}"


DELETE_CANCELLED_PROBLEM = "Delete cancelled: admin access was declined."
DELETE_UNAUTHORIZED_PROBLEM = "Delete cancelled: authorization failed."


def _ensure_admin_for_delete(paths: list[Path]) -> str:
    """Prompt for sudo only if any path in the list requires admin privileges.

    Returns "" when the batch may go ahead, otherwise the reason it may not. The
    reason is returned rather than printed for the same repaint reason as the
    removals themselves; the password prompt still writes to the terminal,
    because the user is looking at it while typing.
    """
    admin_paths = [p for p in paths if _needs_admin_for_deletion(p)]
    if not admin_paths:
        return ""

    print()
    if not system.ensure_sudo_session(
        f"{MAGENTA}{MARK_PROMPT}{RESET} File deletion requires admin access\n"
        f"{MAGENTA}{MARK_PROMPT}{RESET} Password: "
    ):
        return DELETE_CANCELLED_PROBLEM if system.SUDO_CANCELLED else DELETE_UNAUTHORIZED_PROBLEM

    system.print_sudo_granted()
    return ""


@dataclass(frozen=True)
class DeleteOutcome:
    """What one Analyze delete batch actually did.

    Analyze repaints a full frame as soon as a batch returns, so the batch cannot
    report by printing -- it hands back these numbers and the screen renders one
    notice line that arrives *with* the next frame instead of under it.

    Truthiness means "something was deleted", which is what the caller uses to
    decide whether the view needs rescanning.
    """

    deleted: int = 0
    failed: int = 0
    freed_bytes: int = 0
    first_problem: str = ""

    def __bool__(self) -> bool:
        return self.deleted > 0


def _delete_analyze_paths(
    paths: list[Path], *, ask_permanent: Callable[[str], bool] | None = None
) -> DeleteOutcome:
    """Delete Analyze targets, using sudo only for paths outside user control."""
    problem = _ensure_admin_for_delete(paths)
    if problem:
        return DeleteOutcome(first_problem=problem)

    admin_paths = [p for p in paths if _needs_admin_for_deletion(p)]
    consent = _permanent_fallback_consent(ask_permanent)
    deleted = 0
    failed = 0
    freed_bytes = 0
    first_problem = ""
    for p in paths:
        removed, size_bytes, reason = (
            _sudo_remove(p)
            if p in admin_paths
            else _safe_remove_analyze_path(p, permanent_consent=consent)
        )
        if removed:
            deleted += 1
            freed_bytes += size_bytes
        else:
            failed += 1
        # The first reason is the one the notice has room for, and a partial
        # success carries one too (a whitelisted root whose caches were cleared
        # while one child was refused).
        if reason and not first_problem:
            first_problem = reason
    if deleted:
        play_delete()
    return DeleteOutcome(
        deleted=deleted, failed=failed, freed_bytes=freed_bytes, first_problem=first_problem
    )


def delete_and_refresh_cache(
    paths: list[Path],
    current_target: Path | None,
    *,
    ask_permanent: Callable[[str], bool] | None = None,
) -> DeleteOutcome:
    """Delete paths and invalidate scan caches on success."""
    outcome = _delete_analyze_paths(paths, ask_permanent=ask_permanent)
    if not outcome:
        return outcome
    if current_target:
        ScanCache.discard(current_target)
    else:
        ScanCache.clear()
    return outcome
