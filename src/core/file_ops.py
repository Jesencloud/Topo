import os
import shutil
import subprocess
import re
from pathlib import Path
from typing import Tuple, Union

from .whitelist import is_protected

def parse_size_from_text(text: str) -> int:
    """Simple parser for sizes like '1.2M', '500B', '2.5 GB' in command output."""
    if not text: return 0
    # Common patterns: 'freed 1.2M', 'total of 500 B', 'reclaimed 2.5 GB'
    match = re.search(r'([0-9.]+)\s*([KMGTB]?B|[KMG])', text, re.IGNORECASE)
    if match:
        val = float(match.group(1))
        unit = match.group(2).upper()
        if 'G' in unit: val *= 1024 * 1024 * 1024
        elif 'M' in unit: val *= 1024 * 1024
        elif 'K' in unit: val *= 1024
        return int(val)
    return 0

def get_size(path: Union[str, Path]) -> int:
    """Recursive size calculation in bytes. Follows symlinks but doesn't recurse into them."""
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
            if entry.is_symlink():
                total += entry.stat().st_size
            elif entry.is_file():
                total += entry.stat().st_size
            elif entry.is_dir():
                total += get_size(entry.path)
    except OSError:
        pass
    return total

def safe_remove(path: Union[str, Path], use_trash: bool = True) -> Tuple[bool, str]:
    """Safe removal with trash support on Linux."""
    path = Path(path).expanduser().resolve()
    
    if not path.exists():
        return False, "Path does not exist"
    
    if is_protected(path):
        return False, "Path is whitelisted"
    
    # Critical system paths protection
    if path in [Path("/"), Path("/usr"), Path("/etc"), Path("/var"), Path.home()]:
        return False, "Refusing to delete critical system path"

    try:
        if use_trash:
            # Try gio trash (Standard GNOME/Freedesktop)
            if shutil.which("gio"):
                res = subprocess.run(["gio", "trash", str(path)], capture_output=True)
                if res.returncode == 0:
                    return True, "Moved to trash (gio)"
            
            # Try trash-put (trash-cli package)
            if shutil.which("trash-put"):
                res = subprocess.run(["trash-put", str(path)], capture_output=True)
                if res.returncode == 0:
                    return True, "Moved to trash (trash-cli)"

        # Fallback to permanent delete if trash fails or not requested
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
        return True, "Permanently deleted"
        
    except Exception as e:
        return False, str(e)

def bytes_to_human(n_bytes: int) -> str:
    """Converts bytes to human readable format (Base-10, same as macOS/Modern Linux)."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if n_bytes < 1000:
            return f"{n_bytes:.1f} {unit}" if unit != 'B' else f"{int(n_bytes)} {unit}"
        n_bytes /= 1000
    return f"{n_bytes:.1f} PB"
