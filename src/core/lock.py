import contextlib
import fcntl
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, NoReturn

from .constants import RESET, YELLOW
from .text import sanitize_for_display

LOCK_FILE_PATH = Path.home() / ".config" / "topo" / "topo.lock"


class SingleInstanceLock:
    """POSIX fcntl file-lock based single instance guard.

    Ensures only one instance of topo clean/uninstall is running
    per user session.
    """

    def __init__(self, lock_file: Path = LOCK_FILE_PATH):
        self.lock_file = lock_file
        self._file_obj: Any = None

    def _fail(self, message: str, hint: str = "") -> NoReturn:
        """Report why the guard could not be taken, then abort."""
        if self._file_obj is not None:
            with contextlib.suppress(OSError):
                self._file_obj.close()
            self._file_obj = None
        print(f"\n {YELLOW}⚠️  {message}{RESET}")
        if hint:
            print(f" {YELLOW}   {hint}{RESET}")
        print()
        sys.exit(1)

    def _holder_pid(self) -> str:
        """Best-effort read of the PID recorded by the instance holding the lock."""
        try:
            with open(self.lock_file, encoding="utf-8", errors="replace") as f:
                recorded = f.read(64).strip()
        except OSError:
            return ""
        return recorded if recorded.isdigit() else ""

    def __enter__(self):
        # A directory that cannot be created is a setup problem, not a second
        # instance; it must not be reported as one.
        try:
            self.lock_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as err:
            self._fail(
                f"Cannot create the lock directory: {sanitize_for_display(str(err))}",
                "Check the permissions of ~/.config/topo and retry.",
            )

        # Opening the lock file and taking the lock fail for entirely different
        # reasons, so they get separate handlers: a permission error or a
        # symlinked lock file used to be announced as "another instance is
        # running", which sent users looking for a process that did not exist.
        flags = os.O_RDWR | os.O_CREAT
        for name in ("O_NOFOLLOW", "O_CLOEXEC"):
            flags |= getattr(os, name, 0)
        try:
            fd = os.open(self.lock_file, flags, 0o600)
        except OSError as err:
            self._fail(
                f"Cannot open the lock file: {sanitize_for_display(str(err))}",
                f"Inspect {self.lock_file} — a symlink there is rejected on purpose.",
            )
        try:
            self._file_obj = open(fd, "r+", closefd=True)
        except OSError as err:
            with contextlib.suppress(OSError):
                os.close(fd)
            self._fail(f"Cannot use the lock file: {sanitize_for_display(str(err))}")

        try:
            fcntl.flock(self._file_obj.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = self._holder_pid()
            owner = f" (PID {holder})" if holder else ""
            self._fail(
                f"Another topo instance{owner} is already running. "
                "Exiting to prevent file conflicts.",
            )
        except OSError as err:
            self._fail(
                f"Cannot acquire the instance lock: {sanitize_for_display(str(err))}",
                "This filesystem may not support advisory locks.",
            )

        try:
            self._file_obj.seek(0)
            self._file_obj.truncate()
            self._file_obj.write(f"{os.getpid()}\n")
            self._file_obj.flush()
        except OSError:
            # The lock itself is held; failing to record our PID only costs the
            # diagnostic above and must not abort the run.
            pass
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._file_obj:
            try:
                fcntl.flock(self._file_obj.fileno(), fcntl.LOCK_UN)
                self._file_obj.close()
            except OSError:
                pass


def is_file_locked(file_path: Path) -> bool:
    """Return True when another process holds a POSIX record lock on *file_path*.

    POSIX record locks (``fcntl.lockf`` / ``F_SETLK``) and BSD advisory locks
    (``flock``) are independent lock spaces on Linux, so the previous ``flock``
    probe could never see the locks actually taken by SQLite — and therefore by
    Chrome, Firefox and Thunderbird. A *shared* probe is used deliberately: it
    conflicts with a writer's exclusive lock (the case worth skipping) while an
    exclusive probe would also require write access, which read-only files and
    read-only mounts do not grant.
    """
    if not file_path.is_file() or file_path.is_symlink():
        return False

    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_CLOEXEC"):
        flags |= getattr(os, name, 0)
    try:
        fd = os.open(file_path, flags)
    except OSError:
        # Being unable to open a file is not evidence that it is in use, and
        # treating it as "locked" made every 0444 file permanently undeletable.
        return False
    try:
        fcntl.lockf(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        fcntl.lockf(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def is_sqlite_busy(db_path: Path) -> bool:
    """Return True when an SQLite database cannot currently take a write lock.

    Asks SQLite's own locking protocol instead of guessing at the lock space,
    which is the only reliable answer for database maintenance.
    """
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=rw", uri=True, timeout=0)
    except sqlite3.Error:
        return True
    try:
        conn.execute("BEGIN IMMEDIATE").close()
        conn.rollback()
        return False
    except sqlite3.Error:
        return True
    finally:
        with contextlib.suppress(Exception):
            conn.close()
