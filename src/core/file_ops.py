import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .system import run_command
from .whitelist import (
    CRITICAL_PREFIX_PATHS,
    DELETION_CRITICAL_EXACT_PATHS,
    get_hard_protection_reason,
    is_protected,
)

# Global registry to track handled paths across modules
CLEANED_PATHS: set[str] = set()
CACHEDIR_TAG_FILE = "CACHEDIR.TAG"
CACHEDIR_TAG_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_SYSTEM_CLEANABLE_CONTENT_DIRS = (Path("/var/tmp"), Path("/var/cache"))


def _is_system_cleanable_content(path: Path) -> bool:
    """Allow contents of known system cache/temp roots without allowing the roots."""
    return any(root in path.parents for root in _SYSTEM_CLEANABLE_CONTENT_DIRS)


def get_deletion_log_path() -> Path:
    """Return the audit log path for destructive file operations."""
    if override := os.environ.get("TOPO_DELETE_LOG"):
        return Path(override).expanduser()
    state_home = Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser()
    return state_home / "topo" / "deletions.log"


def _sanitize_audit_field(value: str) -> str:
    """Escape characters that could forge or corrupt a tab-separated log line.

    A rejected deletion target may contain control characters (it can be
    rejected *for* containing them), and that raw value is still logged. Without
    escaping, an embedded newline would inject a forged audit record and a tab
    would shift the column layout.
    """
    return (
        value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    )


def record_deletion_audit(
    path: str | Path,
    mode: str,
    status: str,
    size_bytes: int | None = None,
) -> None:
    """Append a best-effort deletion audit event."""
    log_path = get_deletion_log_path()
    try:
        size = "unknown" if size_bytes is None else str(max(int(size_bytes), 0))
    except (TypeError, ValueError):
        size = "unknown"
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as f:
            safe_path = _sanitize_audit_field(str(Path(path).expanduser()))
            f.write(f"{timestamp}\t{mode}\t{size}\t{status}\t{safe_path}\n")
    except OSError:
        pass


def validate_path_for_deletion(
    path: str | Path,
    allow_app_data_removal: bool = False,
) -> tuple[bool, str]:
    """Validate a raw deletion target before size checks or unlink attempts."""
    raw_text = os.fspath(path)
    if not raw_text:
        return False, "Path is empty"
    if _CONTROL_CHARS_RE.search(raw_text):
        return False, "Path contains control characters"
    if not Path(raw_text).expanduser().is_absolute():
        return False, "Path must be absolute"
    if any(part == ".." for part in Path(raw_text).parts):
        return False, "Path traversal is not allowed"

    raw_path = Path(raw_text).expanduser()
    try:
        resolved_path = raw_path.resolve(strict=False)
    except OSError:
        resolved_path = raw_path.absolute()

    if allow_app_data_removal:
        if reason := get_hard_protection_reason(resolved_path):
            return False, f"Path is hard-protected: {reason}"
    elif is_protected(resolved_path):
        return False, "Path is whitelisted"
    if resolved_path == Path("/") or resolved_path in DELETION_CRITICAL_EXACT_PATHS:
        return False, "Refusing to delete critical system path"
    for critical in CRITICAL_PREFIX_PATHS:
        if (
            resolved_path == critical or critical in resolved_path.parents
        ) and not _is_system_cleanable_content(resolved_path):
            return False, "Refusing to delete critical system path"
    return True, ""


def register_cleaned_path(path: str | Path | None):
    """Registers a path as handled to avoid double-cleaning."""
    if path:
        p = Path(path).expanduser().resolve()
        CLEANED_PATHS.add(str(p))


def is_app_running(process_name: str) -> bool:
    """Check if an application is currently running."""
    return run_command(["pgrep", "-x", process_name], capture=True, timeout=5).ok


def bytes_to_human(n_bytes: int) -> str:
    """Converts bytes to human readable format using binary units."""
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}" if unit != "B" else f"{int(n_bytes)} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} PiB"


def has_valid_cachedir_tag(path: str | Path) -> bool:
    """Return True when a directory contains a valid CACHEDIR.TAG marker."""
    path = Path(path).expanduser()
    tag_path = path / CACHEDIR_TAG_FILE
    try:
        if (
            path.is_symlink()
            or not path.is_dir()
            or tag_path.is_symlink()
            or not tag_path.is_file()
        ):
            return False
        with tag_path.open("r", encoding="utf-8", errors="ignore") as f:
            return f.read(len(CACHEDIR_TAG_SIGNATURE)) == CACHEDIR_TAG_SIGNATURE
    except OSError:
        return False


def get_size(path: str | Path) -> int:
    """Recursive size calculation in bytes."""
    path = Path(path)
    if not path.exists():
        return 0
    if path.is_file() or path.is_symlink():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    try:
        for entry in os.scandir(path):
            if entry.is_symlink() or entry.is_file():
                total += entry.stat().st_size
            elif entry.is_dir():
                total += get_size(entry.path)
    except OSError:
        pass
    return total


def _coerce_non_negative_size(value: object) -> int | None:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _get_fast_scan_data(path: Path) -> dict[str, Any] | None:
    # Lazy import breaks the analyze <-> file_ops import cycle.
    from .analyze import get_rust_scan_data

    data = get_rust_scan_data(path)
    return data if isinstance(data, dict) else None


def get_size_fast(path: str | Path) -> int:
    """Size of a directory using the Rust engine, falling back to get_size().

    The engine now counts hidden files (skip_hidden=false), so its total matches
    the pure-Python walk while being far faster on huge trees (node_modules, the
    cargo registry, model caches). Files and engine-less environments fall back to
    the exact Python implementation.
    """
    p = Path(path)
    if p.is_dir():
        data = _get_fast_scan_data(p)
        if data is not None:
            return _coerce_non_negative_size(data.get("total_size_bytes")) or 0
    return get_size(p)


def get_direct_child_sizes_fast(path: str | Path) -> dict[str, int] | None:
    """Return immediate child sizes from one Rust scan.

    None means no usable fast scan is available and callers should fall back to
    per-child sizing. An empty dict means the scan succeeded but found no
    non-zero direct children.
    """
    p = Path(path)
    if not p.is_dir():
        return None

    data = _get_fast_scan_data(p)
    if data is None:
        return None

    subdirs = data.get("subdirs")
    if not isinstance(subdirs, dict):
        return None

    child_sizes: dict[str, int] = {}
    for name, size in subdirs.items():
        size_bytes = _coerce_non_negative_size(size)
        if size_bytes is not None:
            child_sizes[str(name)] = size_bytes
    return child_sizes


def safe_remove(
    path: str | Path,
    use_trash: bool = True,
    dry_run: bool = False,
    allow_app_data_removal: bool = False,
    known_size_bytes: int | None = None,
) -> tuple[bool, str]:
    """Safe removal with trash support and protection checks."""
    raw_path = Path(path).expanduser()
    mode = "trash" if use_trash else "permanent"

    valid, reason = validate_path_for_deletion(
        path,
        allow_app_data_removal=allow_app_data_removal,
    )
    if not valid:
        record_deletion_audit(raw_path, mode, "rejected-validation")
        return False, reason

    if not raw_path.exists() and not raw_path.is_symlink():
        record_deletion_audit(raw_path, mode, "missing", 0)
        return False, "Path does not exist"

    if known_size_bytes is None:
        size_bytes = get_size(raw_path)
    else:
        try:
            size_bytes = max(int(known_size_bytes), 0)
        except (TypeError, ValueError):
            size_bytes = get_size(raw_path)
    if dry_run:
        record_deletion_audit(raw_path, mode, "dry-run", size_bytes)
        return True, "Dry run"

    try:
        if use_trash:
            if (
                shutil.which("gio")
                and run_command(["gio", "trash", str(raw_path)], capture=True, timeout=30).ok
            ):
                record_deletion_audit(raw_path, "trash", "trashed-gio", size_bytes)
                return True, "Moved to trash (gio)"
            if (
                shutil.which("trash-put")
                and run_command(["trash-put", str(raw_path)], capture=True, timeout=30).ok
            ):
                record_deletion_audit(raw_path, "trash", "trashed-trash-cli", size_bytes)
                return True, "Moved to trash (trash-cli)"
            record_deletion_audit(raw_path, "trash", "trash-failed", size_bytes)

        if raw_path.is_symlink() or raw_path.is_file():
            raw_path.unlink()
        elif raw_path.is_dir():
            shutil.rmtree(raw_path)
        else:
            raw_path.unlink()
        record_deletion_audit(raw_path, "permanent", "deleted", size_bytes)
        return True, "Permanently deleted"
    except OSError as e:
        failed_mode = "permanent" if use_trash else mode
        record_deletion_audit(raw_path, failed_mode, "failed", size_bytes)
        return False, str(e)


def clean_path_by_age(path: str | Path, days: int, dry_run: bool = False) -> tuple[int, int]:
    """Cleans items within a path that haven't been touched in 'days' days."""
    path = Path(path).expanduser()
    if not path.exists() or not path.is_dir():
        return 0, 0

    total_size = 0
    items_count = 0
    cutoff = time.time() - (days * 86400)

    try:
        entries = list(path.iterdir())
    except OSError:
        return total_size, items_count

    for item in entries:
        try:
            # lstat() judges the entry itself and never follows a symlink to its
            # target. Consider both atime and mtime so that 'noatime'/'relatime'
            # mounts (where atime barely updates) don't make active data look stale.
            st = item.lstat()
        except OSError:
            # A single vanished/broken entry must not abort the whole sweep.
            continue
        if st.st_atime >= cutoff or st.st_mtime >= cutoff:
            continue
        size = get_size(item)
        if dry_run:
            safe_remove(item, use_trash=False, dry_run=True)
            total_size += size
            items_count += 1
        elif safe_remove(item, use_trash=False)[0]:
            total_size += size
            items_count += 1
    return total_size, items_count


def parse_size_to_bytes(text: str) -> int:
    """Parse a human-readable size string as bytes using binary units."""
    if not text or text == "N/A":
        return 0
    match = re.search(r"([0-9.]+)\s*([KMGTPE]?I?B|[KMGTPE])", text, re.IGNORECASE)
    if match:
        val = float(match.group(1))
        unit = match.group(2).upper()
        if "P" in unit:
            val *= 1024**5
        elif "T" in unit:
            val *= 1024**4
        elif "G" in unit:
            val *= 1024**3
        elif "M" in unit:
            val *= 1024**2
        elif "K" in unit:
            val *= 1024
        return int(val)
    # A bare numeric string (no unit) is treated as raw bytes — but only when the
    # whole value is numeric, so stray digits in command output aren't misread.
    stripped = text.strip()
    if stripped and stripped.replace(".", "", 1).isdigit():
        return int(float(stripped))
    return 0


def parse_size_from_text(text: str) -> int:
    """Parser for sizes in command output."""
    return parse_size_to_bytes(text)
