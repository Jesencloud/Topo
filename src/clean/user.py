import contextlib
import os
import shutil
import stat
from pathlib import Path

from ..core.config import get_use_trash
from ..core.constants import (
    CLEAN_BACKUP_AGE_DAYS,
    CLEAN_TEMP_AGE_DAYS,
    OK,
    SKIP,
)
from ..core.file_ops import (
    age_cutoff,
    bytes_to_human,
    clean_path_by_age,
    get_size_fast,
    is_reclaimable_entry_type,
    safe_remove,
)
from ..core.system import run_command


def clean_trash(dry_run=False):
    """Empty Linux trash (supports gio and common trash dirs)."""
    total_size = 0
    total_items = 0

    # 1. Try with gio (preferred on GNOME/modern desktops)
    if shutil.which("gio"):
        trash_path = Path.home() / ".local/share/Trash"
        size = get_size_fast(trash_path) if trash_path.exists() else 0
        if dry_run:
            if size > 0:
                print(f"  {SKIP} User Trash ({bytes_to_human(size)}) would be emptied")
                return size, 1, 1
            return 0, 0, 0

        res = run_command(["gio", "trash", "--empty"], capture=True, timeout=30)
        if res.ok:
            print(f"  {OK} User Trash ({bytes_to_human(size)}) emptied")
            return size, 1, 1

    # 2. Fallback: empty the standard home Trash and this user's /tmp trash.
    #    Paths use the real UID (not a literal "$USER"), and removal goes through
    #    safe_remove so protection/audit apply and stats reflect what truly went.
    trash_dirs = [
        Path.home() / ".local/share/Trash",
        # Linux per-user trash location, not temp file creation.
        Path(f"/tmp/.Trash-{os.getuid()}"),  # nosec B108
    ]
    for td in trash_dirs:
        if not td.exists():
            continue
        size = get_size_fast(td)
        if dry_run:
            if size > 0:
                print(f"  {SKIP} {td} ({bytes_to_human(size)}) would be cleaned")
                total_size += size
                total_items += 1
            continue
        if safe_remove(td, use_trash=False)[0]:
            td.mkdir(parents=True, exist_ok=True)
            total_size += size
            total_items += 1
            print(f"  {OK} {td} ({bytes_to_human(size)}) cleaned")

    return total_size, total_items, (1 if total_items > 0 else 0)


def clean_system_temp(dry_run=False, min_age_days=CLEAN_TEMP_AGE_DAYS):
    """Clean stale temporary files from /tmp and /var/tmp.

    Only removes entries that are (a) a regular file, a directory or a symlink,
    (b) owned by the current user and (c) untouched (both mtime and atime) for at
    least ``min_age_days`` days. Sockets, FIFOs and device nodes are left alone:
    none of them hold reclaimable space, and some of them -- the session
    ssh-agent socket above all -- belong to a program that is still running. A
    stale symlink stays in scope, since unlinking it never touches its target.
    """
    total_size = 0
    total_items = 0
    uid = os.getuid()
    cutoff = age_cutoff(min_age_days)

    # Intentional temp cleanup roots, not temp file creation.
    temp_paths = [Path("/tmp"), Path("/var/tmp")]  # nosec B108
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
                # Sockets, FIFOs and device nodes never free space and may well
                # belong to a running program -- the session ssh-agent socket
                # above all. The rule lives in file_ops so the recursive pass
                # below applies exactly the same one.
                if not is_reclaimable_entry_type(st.st_mode):
                    continue
                # Skip files owned by others, and anything still recently active
                if st.st_uid != uid:
                    continue
                if st.st_mtime > cutoff or st.st_atime > cutoff:
                    continue
                if stat.S_ISDIR(st.st_mode):
                    # For directories, clean stale contents individually
                    # instead of deleting the entire tree.
                    s, i = clean_path_by_age(item, days=min_age_days, dry_run=dry_run)
                    total_size += s
                    total_items += i
                    # Remove the directory itself only if now empty
                    if not dry_run:
                        with contextlib.suppress(OSError):
                            item.rmdir()  # Only succeeds if empty
                else:
                    size = get_size_fast(item)
                    if safe_remove(item, use_trash=False, dry_run=dry_run)[0]:
                        total_size += size
                        total_items += 1
        except OSError:
            continue
    if total_items > 0:
        glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
        print(f"  {glyph} Stale temp files ({bytes_to_human(total_size)}) {status}")
        return total_size, total_items, 1
    return 0, 0, 0


def clean_user_logs(dry_run=False):
    """Clean stale or oversized user application log files."""
    total_size = 0
    total_items = 0
    home = Path.home()
    cutoff = age_cutoff(30)  # 30 days

    # ~/.xsession-errors is held open by the X session for the whole login, so
    # unlinking it reclaims nothing until logout (the inode outlives the name)
    # and leaves the session writing to a file that no longer has a path.
    # Truncating releases the blocks immediately and keeps the writer's fd
    # valid -- the same reason logrotate offers "copytruncate".
    live_log = home / ".xsession-errors"
    live_size = get_size_fast(live_log) if live_log.is_file() else 0
    if live_size > 0:
        truncated = True
        if not dry_run:
            try:
                os.truncate(live_log, 0)
            except OSError:
                truncated = False
        if truncated:
            total_size += live_size
            total_items += 1

    # Rotated copies have no writer left, so removing them is the right move.
    known_logs = [
        home / ".xsession-errors.old",
        home / ".local/share/xorg" / "Xorg.0.log.old",
        home / ".local/share/xorg" / "Xorg.1.log.old",
    ]
    for log_file in known_logs:
        if log_file.is_file():
            size = get_size_fast(log_file)
            if size > 0 and (dry_run or safe_remove(log_file, use_trash=False)[0]):
                total_size += size
                total_items += 1

    # Scan app log directories under ~/.local/share/*/logs/
    log_roots = [
        home / ".local" / "share",
        home / ".config",
    ]
    for root in log_roots:
        if not root.is_dir():
            continue
        try:
            for app_dir in root.iterdir():
                if not app_dir.is_dir():
                    continue
                for log_dir_name in ("logs", "log", "Logs"):
                    log_dir = app_dir / log_dir_name
                    if not log_dir.is_dir():
                        continue
                    try:
                        for log_file in log_dir.iterdir():
                            if not log_file.is_file():
                                continue
                            try:
                                st = log_file.stat()
                            except OSError:
                                continue
                            if (
                                st.st_mtime < cutoff
                                and st.st_size > 0
                                and (dry_run or safe_remove(log_file, use_trash=False)[0])
                            ):
                                total_size += st.st_size
                                total_items += 1
                    except OSError:
                        continue
        except OSError:
            continue

    if total_items > 0:
        glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
        print(f"  {glyph} User log files ({bytes_to_human(total_size)}) {status}")
        return total_size, total_items, 1
    return 0, 0, 0


def clean_backup_files(dry_run=False, min_age_days=CLEAN_BACKUP_AGE_DAYS):
    """Clean editor backup and swap files from user directories.

    These are documents, not caches: a ``.bak`` or ``file~`` is often the only
    copy of an earlier revision, and a ``.swp`` is vim's crash-recovery state for
    a buffer that may still be open. So they go to the trash rather than being
    unlinked -- unless config.json turns ``use_trash`` off, which is the one way
    to ask for them to be wiped -- and only once untouched for ``min_age_days``:
    vim rewrites its swap file as you type (see 'updatetime' and 'updatecount'),
    which keeps a live session's mtime well inside the window.
    """
    total_size = 0
    total_items = 0
    home = Path.home()
    use_trash = get_use_trash()
    cutoff = age_cutoff(min_age_days)
    # Only scan common user document directories, not all of home
    scan_dirs = [
        home / "Desktop",
        home / "Documents",
        home / "Downloads",
    ]
    backup_suffixes = {".bak", ".swp", ".swo"}

    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        try:
            for item in scan_dir.rglob("*"):
                if not item.is_file():
                    continue
                # Match *~ files or backup suffixes
                if not (item.name.endswith("~") or item.suffix in backup_suffixes):
                    continue
                try:
                    st = item.stat()
                except OSError:
                    continue
                if st.st_mtime > cutoff:
                    continue
                # st already carries the size for a regular file; asking
                # get_size_fast would just stat it a second time.
                size = st.st_size
                if dry_run or safe_remove(item, use_trash=use_trash)[0]:
                    total_size += size
                    total_items += 1
        except OSError:
            continue

    if total_items > 0:
        if use_trash:
            glyph, status = (SKIP, "would be trashed") if dry_run else (OK, "moved to trash")
        else:
            glyph, status = (SKIP, "would be deleted") if dry_run else (OK, "deleted")
        print(f"  {glyph} Backup/swap files ({bytes_to_human(total_size)}) {status}")
        return total_size, total_items, 1
    return 0, 0, 0


def clean_thumbnails(dry_run=False):
    """Clean desktop image/video thumbnail caches (~/.cache/thumbnails)."""
    thumb_dir = Path.home() / ".cache" / "thumbnails"
    if not thumb_dir.exists():
        return 0, 0, 0
    size = get_size_fast(thumb_dir)
    if size == 0:
        return 0, 0, 0

    if dry_run:
        print(f"  {SKIP} Desktop thumbnail cache ({bytes_to_human(size)}) would be cleaned")
        return size, 1, 1

    if safe_remove(thumb_dir, use_trash=False)[0]:
        thumb_dir.mkdir(parents=True, exist_ok=True)
        print(f"  {OK} Desktop thumbnail cache ({bytes_to_human(size)}) cleaned")
        return size, 1, 1
    return 0, 0, 0


def clean_user_data(dry_run=False):
    """Combined user data cleanup."""
    total_size = 0
    total_items = 0
    categories = 0

    for s, i, c in (
        clean_trash(dry_run),
        clean_system_temp(dry_run),
        clean_user_logs(dry_run),
        clean_backup_files(dry_run),
        clean_thumbnails(dry_run),
    ):
        total_size += s
        total_items += i
        categories += c

    return total_size, total_items, categories
