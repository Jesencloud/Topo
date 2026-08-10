import contextlib
import fcntl
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from .constants import RESET, YELLOW

LOCK_FILE_PATH = Path.home() / ".config" / "topo" / "topo.lock"


class SingleInstanceLock:
    """POSIX fcntl file-lock based single instance guard.

    Ensures only one instance of topo clean/uninstall/purge is running
    per user session.
    """

    def __init__(self, lock_file: Path = LOCK_FILE_PATH):
        self.lock_file = lock_file
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self._file_obj: Any = None

    def __enter__(self):
        try:
            f = open(self.lock_file, "w")
            self._file_obj = f
            # Attempt to acquire exclusive lock without blocking
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            f.write(f"{os.getpid()}\n")
            f.flush()
        except (OSError, BlockingIOError):
            print(
                f"\n {YELLOW}⚠️  Another topo instance is already running. Exiting to prevent file conflicts.{RESET}\n"
            )
            sys.exit(1)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._file_obj:
            try:
                fcntl.flock(self._file_obj.fileno(), fcntl.LOCK_UN)
                self._file_obj.close()
            except OSError:
                pass


def is_file_locked(file_path: Path) -> bool:
    """Checks if a file is currently locked by another process."""
    if not file_path.is_file():
        return False

    try:
        with open(file_path, "r+") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return False
            except (OSError, BlockingIOError):
                return True
    except OSError:
        # Permission denied or unable to open in r+ mode implies potential active use
        return True


def safe_vacuum_sqlite(db_path: Path, timeout: float = 3.0) -> bool:
    """Safely executes VACUUM on an SQLite database without corrupting active DB files.

    If the database is currently opened/locked by an external application (e.g. Chrome, Firefox),
    it safely catches the lock contention and skips instead of crashing or corrupting data.
    """
    if not db_path.is_file():
        return False

    if is_file_locked(db_path):
        print(f"  {YELLOW}◎ Skipping active/locked database: {db_path.name}{RESET}")
        return False

    try:
        conn = sqlite3.connect(str(db_path), timeout=timeout)
        try:
            conn.execute("VACUUM;")
            conn.close()
            return True
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as err:
            # Handles "database is locked", "disk I/O error", etc.
            print(f"  {YELLOW}◎ Skipped locked DB ({db_path.name}): {err}{RESET}")
            return False
        finally:
            with contextlib.suppress(Exception):
                conn.close()
    except Exception:
        return False
