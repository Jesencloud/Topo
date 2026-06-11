import os
import shutil
import time
from pathlib import Path

from ..core.file_ops import bytes_to_human, get_size_fast, safe_remove
from ..core.system import run_command


def clean_trash(dry_run=False):
    """Empty Linux trash (supports gio and common trash dirs)."""
    total_size = 0
    total_items = 0

    # 1. Try with gio (preferred on GNOME/modern desktops)
    if shutil.which("gio"):
        if dry_run:
            # We need to estimate size
            trash_path = Path.home() / ".local/share/Trash"
            if trash_path.exists():
                size = get_size_fast(trash_path)
                if size > 0:
                    print(
                        f"  \033[0;32m✓\033[0m User Trash ({bytes_to_human(size)}) would be emptied"
                    )
                    return size, 1, 1
            return 0, 0, 0

        res = run_command(["gio", "trash", "--empty"], capture=True, timeout=30)
        if res.ok:
            print("  \033[0;32m✓\033[0m User Trash emptied")
            return 0, 1, 1

    # 2. Fallback: empty the standard home Trash and this user's /tmp trash.
    #    Paths use the real UID (not a literal "$USER"), and removal goes through
    #    safe_remove so protection/audit apply and stats reflect what truly went.
    trash_dirs = [
        Path.home() / ".local/share/Trash",
        Path(f"/tmp/.Trash-{os.getuid()}"),
    ]
    for td in trash_dirs:
        if not td.exists():
            continue
        size = get_size_fast(td)
        if dry_run:
            if size > 0:
                print(f"  \033[0;32m✓\033[0m {td} ({bytes_to_human(size)}) would be cleaned")
                total_size += size
                total_items += 1
            continue
        if safe_remove(td, use_trash=False)[0]:
            td.mkdir(parents=True, exist_ok=True)
            total_size += size
            total_items += 1
            print(f"  \033[0;32m✓\033[0m {td} ({bytes_to_human(size)}) cleaned")

    return total_size, total_items, (1 if total_items > 0 else 0)


def clean_system_temp(dry_run=False, min_age_days=3):
    """Clean stale temporary files from /tmp and /var/tmp.

    Only removes entries that are (a) owned by the current user and (b) untouched
    (both mtime and atime) for at least ``min_age_days`` days. This avoids deleting
    sockets, locks and scratch files that belong to running programs or other users.
    """
    total_size = 0
    total_items = 0
    uid = os.getuid()
    cutoff = time.time() - (min_age_days * 86400)

    temp_paths = [Path("/tmp"), Path("/var/tmp")]
    for path in temp_paths:
        if not path.exists():
            continue
        try:
            for item in path.iterdir():
                # Avoid hidden files and systemd's private temp trees
                if item.name.startswith(".") or "systemd" in item.name:
                    continue
                try:
                    st = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                # Skip files owned by others, and anything still recently active
                if st.st_uid != uid:
                    continue
                if st.st_mtime > cutoff or st.st_atime > cutoff:
                    continue
                size = get_size_fast(item)
                if safe_remove(item, use_trash=False, dry_run=dry_run)[0]:
                    total_size += size
                    total_items += 1
        except OSError:
            continue
    if total_items > 0:
        status = "would be cleaned" if dry_run else "cleaned"
        print(f"  \033[0;32m✓\033[0m Stale temp files ({bytes_to_human(total_size)}) {status}")
        return total_size, total_items, 1
    return 0, 0, 0


def clean_user_data(dry_run=False):
    """Combined user data cleanup."""
    total_size = 0
    total_items = 0
    categories = 0

    s, i, c = clean_trash(dry_run)
    total_size += s
    total_items += i
    categories += c

    s, i, c = clean_system_temp(dry_run)
    total_size += s
    total_items += i
    categories += c

    return total_size, total_items, categories
