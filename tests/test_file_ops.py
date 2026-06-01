import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.file_ops import (
    CLEANED_PATHS,
    bytes_to_human,
    clean_path_by_age,
    get_deletion_log_path,
    get_size,
    is_app_running,
    parse_size_from_text,
    parse_size_to_bytes,
    register_cleaned_path,
    safe_remove,
)
from src.core.whitelist import is_protected


def test_register_cleaned_path():
    CLEANED_PATHS.clear()
    register_cleaned_path(Path("/tmp/test_path"))
    assert "/tmp/test_path" in CLEANED_PATHS
    register_cleaned_path(None)  # Should not fail


@patch("subprocess.run")
def test_is_app_running(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    assert is_app_running("test_app") is True

    mock_run.return_value = MagicMock(returncode=1)
    assert is_app_running("test_app") is False

    mock_run.side_effect = OSError("error")
    assert is_app_running("test_app") is False


def test_whitelist_protection(test_env):
    """Verify that critical system paths are protected."""
    assert is_protected("/") is True
    assert is_protected("/usr/bin") is True
    assert is_protected("/etc/shadow") is True
    assert is_protected("/boot") is True
    assert is_protected("/run/systemd") is True
    assert is_protected("/var") is True
    assert is_protected("/var/log/journal") is True
    assert is_protected("/var/tmp") is True
    assert is_protected("/var/cache") is True
    assert is_protected("/var/tmp/topo-stale.tmp") is False
    assert is_protected("/var/cache/dnf/topo-cache") is False
    assert is_protected(test_env / "my_docs") is False


def test_safe_remove_prevents_system_deletion(test_env):
    """Verify safe_remove refuses to delete protected paths."""
    success, message = safe_remove("/", use_trash=False)
    assert success is False
    assert "whitelisted" in message.lower()


def test_safe_remove_prevents_sensitive_linux_app_data(test_env):
    profile_dir = test_env / ".mozilla/firefox/profile.default"
    profile_dir.mkdir(parents=True)
    login_db = profile_dir / "logins.json"
    login_db.write_text("{}")

    success, message = safe_remove(profile_dir, use_trash=False)

    assert success is False
    assert "whitelisted" in message.lower()
    assert login_db.exists()


def test_safe_remove_deletion(test_env):
    """Verify safe_remove works for non-protected test files."""
    test_file = test_env / "temp_artifact.log"
    test_file.write_text("dummy data")

    assert test_file.exists()
    success, message = safe_remove(test_file, use_trash=False)

    assert success is True
    assert not test_file.exists()


def test_safe_remove_writes_deletion_audit(test_env, monkeypatch):
    log_path = test_env / "state" / "topo" / "deletions.log"
    monkeypatch.setenv("TOPO_DELETE_LOG", str(log_path))
    test_file = test_env / "audit.log"
    test_file.write_text("audit")

    success, message = safe_remove(test_file, use_trash=False)

    assert success is True
    assert "Permanently deleted" in message
    line = log_path.read_text().strip()
    fields = line.split("\t")
    assert fields[1:] == ["permanent", "5", "deleted", str(test_file)]


def test_safe_remove_dry_run_audit_keeps_file(test_env, monkeypatch):
    log_path = test_env / "state" / "topo" / "deletions.log"
    monkeypatch.setenv("TOPO_DELETE_LOG", str(log_path))
    test_file = test_env / "dry-run.log"
    test_file.write_text("preview")

    success, message = safe_remove(test_file, use_trash=False, dry_run=True)

    assert success is True
    assert message == "Dry run"
    assert test_file.exists()
    line = log_path.read_text().strip()
    fields = line.split("\t")
    assert fields[1:] == ["permanent", "7", "dry-run", str(test_file)]


def test_deletion_log_defaults_to_xdg_state_home(test_env, monkeypatch):
    state_home = test_env / "xdg-state"
    monkeypatch.delenv("TOPO_DELETE_LOG", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    assert get_deletion_log_path() == state_home / "topo" / "deletions.log"


def test_get_size_accurate(test_env):
    """Verify file size calculation."""
    test_file = test_env / "size_test.bin"
    content = b"0" * 1024  # 1KB
    test_file.write_bytes(content)

    assert get_size(test_file) == 1024

    test_dir = test_env / "size_dir"
    test_dir.mkdir()
    (test_dir / "f1").write_bytes(b"0" * 500)
    (test_dir / "f2").write_bytes(b"0" * 524)

    assert get_size(test_dir) == 1024


def test_get_size_error_handling():
    # Non-existent path
    assert get_size(Path("/tmp/this_should_never_exist_12345")) == 0

    # Mock OSError during stat AND scandir
    with (
        patch("pathlib.Path.is_file", return_value=True),
        patch("pathlib.Path.stat", side_effect=OSError),
    ):
        assert get_size(Path("/tmp")) == 0

    with (
        patch("pathlib.Path.is_file", return_value=False),
        patch("pathlib.Path.is_symlink", return_value=False),
        patch("os.scandir", side_effect=OSError),
    ):
        assert get_size(Path("/tmp")) == 0


def test_bytes_to_human():
    assert bytes_to_human(500) == "500 B"
    assert bytes_to_human(1024) == "1.0 KiB"
    assert bytes_to_human(1536 * 1024) == "1.5 MiB"
    assert bytes_to_human(int(1.2 * 1024**3)) == "1.2 GiB"
    assert bytes_to_human(5 * 1024**4) == "5.0 TiB"


def test_parse_size_from_text():
    assert parse_size_from_text("freed 1.5 GB of space") == int(1.5 * 1024**3)
    assert parse_size_from_text("total 500 MB") == int(500 * 1024**2)
    assert parse_size_from_text("10 KB used") == int(10 * 1024)
    assert parse_size_to_bytes("1.5 GiB") == int(1.5 * 1024**3)
    assert parse_size_from_text("no size here") == 0
    assert parse_size_from_text("") == 0


def test_safe_remove_edge_cases(test_env):
    # Test non-existent file
    success, msg = safe_remove(test_env / "non_existent.txt")
    assert success is False
    assert "not exist" in msg

    # Test critical paths fallback protection
    with patch("src.core.file_ops.is_protected", return_value=False):
        success, msg = safe_remove(Path("/"))
        assert success is False
        assert "critical system path" in msg.lower()

    # Test fallback to permanent delete if trash fails
    test_file = test_env / "trash_test.txt"
    test_file.write_text("dummy")
    log_path = test_env / "state" / "topo" / "deletions.log"
    with (
        patch.dict("os.environ", {"TOPO_DELETE_LOG": str(log_path)}),
        patch("shutil.which", return_value=True),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=1)  # But it fails
        success, msg = safe_remove(test_file, use_trash=True)
        assert success is True
        assert "Permanently deleted" in msg
    lines = log_path.read_text().splitlines()
    assert lines[0].split("\t")[1:] == ["trash", "5", "trash-failed", str(test_file)]
    assert lines[1].split("\t")[1:] == ["permanent", "5", "deleted", str(test_file)]

    # Test Exception handling during removal
    with patch("pathlib.Path.unlink", side_effect=OSError("mocked error")):
        test_file = test_env / "err_test.txt"
        test_file.write_text("dummy")
        success, msg = safe_remove(test_file, use_trash=False)
        assert success is False
        assert "mocked error" in msg


def test_safe_remove_deletes_symlink_not_target(test_env):
    target_dir = test_env / "target"
    target_dir.mkdir()
    target_file = target_dir / "kept.txt"
    target_file.write_text("keep")
    link = test_env / "target-link"
    link.symlink_to(target_dir, target_is_directory=True)

    success, msg = safe_remove(link, use_trash=False)

    assert success is True
    assert "Permanently deleted" in msg
    assert not link.exists()
    assert target_dir.exists()
    assert target_file.exists()


def test_safe_remove_deletes_broken_symlink(test_env):
    link = test_env / "broken-link"
    link.symlink_to(test_env / "missing-target")

    success, msg = safe_remove(link, use_trash=False)

    assert success is True
    assert "Permanently deleted" in msg
    assert not link.is_symlink()


def test_safe_remove_respects_parent_whitelist(test_env):
    parent = test_env / "protected"
    parent.mkdir()
    child = parent / "child.txt"
    child.write_text("keep")

    with patch("src.core.file_ops.is_protected", return_value=True):
        success, msg = safe_remove(child, use_trash=False)

    assert success is False
    assert "whitelisted" in msg
    assert child.exists()


def test_safe_remove_reports_permission_error(test_env):
    test_file = test_env / "readonly.txt"
    test_file.write_text("data")

    with patch("pathlib.Path.unlink", side_effect=PermissionError("denied")):
        success, msg = safe_remove(test_file, use_trash=False)

    assert success is False
    assert "denied" in msg


def test_clean_path_by_age(test_env):
    cache_dir = test_env / "cache"
    cache_dir.mkdir()
    f1 = cache_dir / "old_file.txt"
    f2 = cache_dir / "new_file.txt"
    f1.write_text("old")
    f2.write_text("new")

    current_time = time.time()
    old_time = current_time - (15 * 86400)

    # We mock the entire stat object returned by iterdir()
    with patch("pathlib.Path.stat") as mock_stat:
        mock_stat.return_value.st_atime = old_time
        # Since we mock Path.stat universally, both files look old

        # Dry run
        size, items = clean_path_by_age(cache_dir, days=10, dry_run=True)
        assert items == 2

        # Real run
        with patch("pathlib.Path.unlink") as mock_unlink:
            size, items = clean_path_by_age(cache_dir, days=10, dry_run=False)
            assert items == 2
            assert mock_unlink.call_count == 2

    # Test OSError handling
    with patch("pathlib.Path.iterdir", side_effect=OSError):
        size, items = clean_path_by_age(cache_dir, days=10)
        assert size == 0
        assert items == 0
