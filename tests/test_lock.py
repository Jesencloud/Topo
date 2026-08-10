import fcntl
import os
import sqlite3

from src.core.lock import SingleInstanceLock, is_file_locked, safe_vacuum_sqlite


def test_single_instance_lock_success(tmp_path):
    """Test acquiring and releasing single instance lock."""
    lock_path = tmp_path / "test.lock"
    with SingleInstanceLock(lock_file=lock_path):
        assert lock_path.exists()
        pid = lock_path.read_text().strip()
        assert pid == str(os.getpid())


def test_is_file_locked(tmp_path):
    """Test detecting locked files."""
    test_file = tmp_path / "sample.txt"
    test_file.write_text("hello")

    assert not is_file_locked(test_file)

    # Lock file with fcntl
    with open(test_file, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            assert is_file_locked(test_file)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def test_safe_vacuum_sqlite(tmp_path):
    """Test safe SQLite VACUUM on unlocked and locked databases."""
    db_file = tmp_path / "test.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE t (id INT);")
    conn.execute("INSERT INTO t VALUES (1);")
    conn.commit()
    conn.close()

    # Unlocked VACUUM should succeed
    assert safe_vacuum_sqlite(db_file)

    # Locked VACUUM should fail gracefully without crashing
    with open(db_file, "r+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            assert not safe_vacuum_sqlite(db_file)
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
