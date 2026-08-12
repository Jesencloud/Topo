import fcntl
import os
import sqlite3
import stat
from pathlib import Path

import pytest
from lock_helpers import RECORD_LOCK_HOLDER, external_holder

from src.core.lock import (
    SingleInstanceLock,
    is_file_locked,
    is_sqlite_busy,
    safe_vacuum_sqlite,
)

_SQLITE_WRITER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], timeout=0)
conn.execute("BEGIN IMMEDIATE")
conn.execute("INSERT INTO t VALUES (99)")
sys.stdout.write("ready\\n")
sys.stdout.flush()
sys.stdin.readline()
"""

# SingleInstanceLock uses flock(), which is owned by the open file description and
# would therefore look free to a same-process probe as well — so the contention
# case also needs a child process holding the guard.
_INSTANCE_LOCK_HOLDER = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
f = open(fd, "r+", closefd=True)
fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
f.seek(0); f.truncate(); f.write(str(os.getpid()) + "\\n"); f.flush()
sys.stdout.write("ready\\n")
sys.stdout.flush()
sys.stdin.readline()
"""


def test_single_instance_lock_success(tmp_path):
    """Test acquiring and releasing single instance lock."""
    lock_path = tmp_path / "test.lock"
    with SingleInstanceLock(lock_file=lock_path):
        assert lock_path.exists()
        pid = lock_path.read_text().strip()
        assert pid == str(os.getpid())


def test_single_instance_lock_creates_private_directory(tmp_path):
    """The lock directory is created lazily, with 0700, by __enter__."""
    lock_path = tmp_path / "nested" / "topo.lock"
    assert not lock_path.parent.exists()
    with SingleInstanceLock(lock_file=lock_path):
        assert lock_path.parent.is_dir()
        assert stat.S_IMODE(lock_path.parent.stat().st_mode) == 0o700


def test_single_instance_lock_reports_holder_pid(tmp_path, capsys):
    """A genuine second instance is named as such, with the holder's PID."""
    lock_path = tmp_path / "held.lock"
    with (
        external_holder(_INSTANCE_LOCK_HOLDER, lock_path),
        pytest.raises(SystemExit) as exc,
    ):
        SingleInstanceLock(lock_file=lock_path).__enter__()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Another topo instance" in out
    assert "already running" in out
    assert "PID" in out


def test_single_instance_lock_rejects_symlinked_lock_file(tmp_path, capsys):
    """A symlinked lock file is refused — and not misreported as a second instance."""
    real = tmp_path / "elsewhere.lock"
    real.write_text("")
    link = tmp_path / "topo.lock"
    link.symlink_to(real)

    with pytest.raises(SystemExit) as exc:
        SingleInstanceLock(lock_file=link).__enter__()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Cannot open the lock file" in out
    assert "Another topo instance" not in out


def test_single_instance_lock_reports_undecipherable_directory(tmp_path, capsys):
    """A lock directory that cannot be created is a setup error, not contention."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    lock_path = blocker / "sub" / "topo.lock"

    with pytest.raises(SystemExit) as exc:
        SingleInstanceLock(lock_file=lock_path).__enter__()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "Cannot create the lock directory" in out
    assert "Another topo instance" not in out


def test_single_instance_lock_fd_is_close_on_exec(tmp_path):
    """The lock fd must not leak into subprocesses (it would keep the lock alive)."""
    lock_path = tmp_path / "cloexec.lock"
    with SingleInstanceLock(lock_file=lock_path) as guard:
        fd = guard._file_obj.fileno()
        assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC


def test_is_file_locked_idle_file(tmp_path):
    """An untouched regular file is never reported as locked."""
    sample = tmp_path / "sample.txt"
    sample.write_text("hello")
    assert not is_file_locked(sample)


def test_is_file_locked_detects_external_record_lock(tmp_path):
    """A POSIX record lock held by another process must be visible.

    This is the lock space SQLite (and therefore Chrome/Firefox/Thunderbird)
    actually uses; the previous flock-based probe could never see it.
    """
    sample = tmp_path / "held.txt"
    sample.write_text("payload")
    with external_holder(RECORD_LOCK_HOLDER, sample):
        assert is_file_locked(sample)
    assert not is_file_locked(sample)


def test_is_file_locked_readonly_file_is_not_locked(tmp_path):
    """A 0444 file is unreadable-for-write, not in use (L-4 regression)."""
    sample = tmp_path / "readonly.txt"
    sample.write_text("immutable")
    sample.chmod(0o444)
    try:
        assert not is_file_locked(sample)
    finally:
        sample.chmod(0o644)


def test_is_file_locked_unopenable_file_is_not_locked(tmp_path):
    """Failing to open a file is not evidence that it is in use."""
    sample = tmp_path / "no-perm.txt"
    sample.write_text("secret")
    sample.chmod(0o000)
    try:
        assert not is_file_locked(sample)
    finally:
        sample.chmod(0o644)


def test_is_file_locked_rejects_symlinks_and_dirs(tmp_path):
    """Only regular files are probed; symlinks and directories short-circuit."""
    target = tmp_path / "target.txt"
    target.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert not is_file_locked(link)
    assert not is_file_locked(tmp_path)
    assert not is_file_locked(tmp_path / "missing.txt")


def _make_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE t (id INT);")
    conn.execute("INSERT INTO t VALUES (1);")
    conn.commit()
    conn.close()


def test_is_sqlite_busy_idle_database(tmp_path):
    """An idle database can take a write lock, so it is not busy."""
    db_file = tmp_path / "idle.db"
    _make_db(db_file)
    assert not is_sqlite_busy(db_file)


def test_is_sqlite_busy_detects_external_writer(tmp_path):
    """An external BEGIN IMMEDIATE transaction makes the database busy."""
    db_file = tmp_path / "busy.db"
    _make_db(db_file)
    with external_holder(_SQLITE_WRITER, db_file):
        assert is_sqlite_busy(db_file)
    assert not is_sqlite_busy(db_file)


def test_is_sqlite_busy_on_unopenable_database(tmp_path):
    """A database that cannot be opened read-write is treated as busy."""
    assert is_sqlite_busy(tmp_path / "does-not-exist.db")


def test_safe_vacuum_sqlite(tmp_path):
    """Test safe SQLite VACUUM on unlocked and externally locked databases."""
    db_file = tmp_path / "test.db"
    _make_db(db_file)

    # Unlocked VACUUM should succeed
    assert safe_vacuum_sqlite(db_file)

    # An active external writer must be skipped, not crashed into
    with external_holder(_SQLITE_WRITER, db_file):
        assert not safe_vacuum_sqlite(db_file)

    # Once the writer is gone the database is maintainable again
    assert safe_vacuum_sqlite(db_file)


def test_safe_vacuum_sqlite_missing_file(tmp_path):
    """A non-existent database is not vacuumed."""
    assert not safe_vacuum_sqlite(tmp_path / "nope.db")
