import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from .whitelist import is_protected

# Global registry to track handled paths across modules
CLEANED_PATHS: set[str] = set()


def register_cleaned_path(path: str | Path | None):
    """Registers a path as handled to avoid double-cleaning."""
    if path:
        p = Path(path).expanduser().resolve()
        CLEANED_PATHS.add(str(p))


def is_app_running(process_name: str) -> bool:
    """Check if an application is currently running."""
    try:
        res = subprocess.run(["pgrep", "-x", process_name], capture_output=True)
        return res.returncode == 0
    except Exception:
        return False


def bytes_to_human(n_bytes: int) -> str:
    """Converts bytes to human readable format using binary units."""
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if n_bytes < 1024:
            return f"{n_bytes:.1f} {unit}" if unit != "B" else f"{int(n_bytes)} {unit}"
        n_bytes /= 1024
    return f"{n_bytes:.1f} PiB"


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


def get_size_fast(path: str | Path) -> int:
    """Size of a directory using the Rust engine, falling back to get_size().

    The engine now counts hidden files (skip_hidden=false), so its total matches
    the pure-Python walk while being far faster on huge trees (node_modules, the
    cargo registry, model caches). Files and engine-less environments fall back to
    the exact Python implementation.
    """
    p = Path(path)
    if p.is_dir():
        # Lazy import breaks the analyze <-> file_ops import cycle.
        from .analyze import get_rust_scan_data

        data = get_rust_scan_data(p)
        if data is not None:
            return data.get("total_size_bytes", 0)
    return get_size(p)


def safe_remove(path: str | Path, use_trash: bool = True) -> tuple[bool, str]:
    """Safe removal with trash support and protection checks."""
    raw_path = Path(path).expanduser()
    try:
        resolved_path = raw_path.resolve(strict=False)
    except OSError:
        resolved_path = raw_path.absolute()

    if not raw_path.exists() and not raw_path.is_symlink():
        return False, "Path does not exist"
    if is_protected(resolved_path):
        return False, "Path is whitelisted"

    # Critical system paths protection
    if resolved_path in [Path("/"), Path("/usr"), Path("/etc"), Path("/var"), Path.home()]:
        return False, "Refusing to delete critical system path"

    try:
        if use_trash:
            if (
                shutil.which("gio")
                and subprocess.run(["gio", "trash", str(raw_path)], capture_output=True).returncode == 0
            ):
                return True, "Moved to trash (gio)"
            if (
                shutil.which("trash-put")
                and subprocess.run(["trash-put", str(raw_path)], capture_output=True).returncode == 0
            ):
                return True, "Moved to trash (trash-cli)"

        if raw_path.is_symlink() or raw_path.is_file():
            raw_path.unlink()
        elif raw_path.is_dir():
            shutil.rmtree(raw_path)
        else:
            raw_path.unlink()
        return True, "Permanently deleted"
    except Exception as e:
        return False, str(e)


def clean_path_by_age(path: str | Path, days: int, dry_run: bool = False) -> tuple[int, int]:
    """Cleans items within a path that haven't been accessed in 'days' days."""
    path = Path(path).expanduser()
    if not path.exists() or not path.is_dir():
        return 0, 0

    total_size = 0
    items_count = 0
    now = time.time()
    cutoff = now - (days * 86400)

    try:
        for item in path.iterdir():
            if item.stat().st_atime < cutoff:
                size = get_size(item)
                if dry_run:
                    total_size += size
                    items_count += 1
                else:
                    if item.is_dir():
                        shutil.rmtree(item, ignore_errors=True)
                    else:
                        item.unlink(missing_ok=True)
                    total_size += size
                    items_count += 1
    except OSError:
        pass
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
    return 0


def parse_size_from_text(text: str) -> int:
    """Parser for sizes in command output."""
    return parse_size_to_bytes(text)
