from src.core.file_ops import get_size, safe_remove
from src.core.whitelist import is_protected


def test_whitelist_protection():
    """Verify that critical system paths are protected."""
    assert is_protected("/") is True
    assert is_protected("/usr/bin") is True
    assert is_protected("/etc/shadow") is True
    assert is_protected("/boot") is True
    assert is_protected("/home/user/my_docs") is False


def test_safe_remove_prevents_system_deletion(test_env):
    """Verify safe_remove refuses to delete protected paths."""
    success, message = safe_remove("/", use_trash=False)
    assert success is False
    assert "whitelisted" in message.lower()


def test_safe_remove_deletion(test_env):
    """Verify safe_remove works for non-protected test files."""
    test_file = test_env / "temp_artifact.log"
    test_file.write_text("dummy data")

    assert test_file.exists()
    success, message = safe_remove(test_file, use_trash=False)

    assert success is True
    assert not test_file.exists()


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
