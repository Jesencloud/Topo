import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.analyze import (
    ScanCache,
    _delete_analyze_paths,
    _needs_admin_for_deletion,
    _render_scan_header,
    _scan_status_message,
    build_analysis_entry,
    get_rust_scan_data,
)
from src.core.file_ops import CACHEDIR_TAG_SIGNATURE, has_valid_cachedir_tag


def test_scan_cache():
    """Verify that ScanCache stores and retrieves data correctly."""
    path = Path("/tmp/test_path")
    data = {"total_size_bytes": 1024}

    ScanCache.set(path, data)
    assert ScanCache.get(path) == data

    # Check that a different path returns None
    assert ScanCache.get(Path("/tmp/other")) is None


@patch("subprocess.run")
def test_get_rust_scan_data_success(mock_run):
    """Verify parsing of Rust engine output."""
    mock_data = {
        "path": "/home/user",
        "total_size_bytes": 5000,
        "file_count": 10,
        "subdirs": {"docs": 2000, "pics": 3000},
        "top_files": [],
    }

    # Mock successful subprocess run
    mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(mock_data))

    # We need to mock Path.exists for the binary
    with patch("pathlib.Path.exists", return_value=True):
        result = get_rust_scan_data(Path("/home/user"))
        assert result == mock_data
        # Verify it was cached
        assert ScanCache.get(Path("/home/user")) == mock_data


def test_has_valid_cachedir_tag(test_env):
    cache_dir = test_env / "cache-dir"
    cache_dir.mkdir()
    (cache_dir / "CACHEDIR.TAG").write_text(f"{CACHEDIR_TAG_SIGNATURE}\nextra metadata")

    assert has_valid_cachedir_tag(cache_dir) is True


def test_scan_status_message_uses_spinner_frame():
    scan_msg = _scan_status_message("scan", "Home", "⠋")
    refresh_msg = _scan_status_message("refresh", "Downloads", "⠙")

    assert scan_msg == "   ⠋ Rust Engine: Analyzing disk usage, please wait . . ."
    assert refresh_msg == "   ⠙ Refreshing analysis on Downloads..."
    assert "🚀" not in scan_msg


def test_render_scan_header_clears_screen_and_prints_title(capsys):
    _render_scan_header("Analyze Disk")

    output = capsys.readouterr().out
    assert output.startswith("\033[2J\033[H")
    assert "Analyze Disk" in output


def test_has_valid_cachedir_tag_rejects_invalid_or_missing_tag(test_env):
    invalid_dir = test_env / "invalid-cache"
    invalid_dir.mkdir()
    (invalid_dir / "CACHEDIR.TAG").write_text("not a cache tag")

    normal_dir = test_env / "normal"
    normal_dir.mkdir()

    assert has_valid_cachedir_tag(invalid_dir) is False
    assert has_valid_cachedir_tag(normal_dir) is False


def test_build_analysis_entry_marks_cachedir_tag_as_cleanable(test_env):
    cache_dir = test_env / "cache-dir"
    cache_dir.mkdir()
    (cache_dir / "CACHEDIR.TAG").write_text(f"{CACHEDIR_TAG_SIGNATURE}\n")

    entry = build_analysis_entry("cache-dir", cache_dir, size=512, total_size=1024)

    assert entry["is_cleanable"] is True
    assert entry["cleanable_reason"] == "CACHEDIR.TAG"
    assert entry["icon"] == "🧹"
    assert entry["percent"] == 50


def test_analyze_delete_user_writable_path_without_admin(test_env):
    target = test_env / "owned-file.txt"
    target.write_text("remove me")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.core.analyze._confirm_delete", return_value=True) as mock_confirm,
        patch("src.core.analyze.safe_remove", return_value=(True, "Moved to trash")) as mock_safe,
        patch("src.core.analyze._sudo_remove") as mock_sudo,
    ):
        assert _delete_analyze_paths([target]) is True

    mock_confirm.assert_called_once_with(1, target.stat().st_size, False)
    mock_safe.assert_called_once_with(target, use_trash=True)
    mock_sudo.assert_not_called()


def test_analyze_delete_system_path_requires_admin():
    target = Path("/var/cache/topo-test")

    with (
        patch("src.core.analyze.get_size", return_value=4096),
        patch("src.core.analyze._confirm_delete", return_value=True) as mock_confirm,
        patch("src.core.analyze.safe_remove") as mock_safe,
        patch("src.core.analyze._sudo_remove", return_value=True) as mock_sudo,
    ):
        assert _delete_analyze_paths([target]) is True

    mock_confirm.assert_called_once_with(1, 4096, True)
    mock_sudo.assert_called_once_with(target)
    mock_safe.assert_not_called()


def test_needs_admin_for_deletion_rejects_non_home_path():
    assert _needs_admin_for_deletion(Path("/usr/share/topo-test")) is True
