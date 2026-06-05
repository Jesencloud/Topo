import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.analyze import (
    ScanCache,
    _delete_analyze_paths,
    _needs_admin_for_deletion,
    _prime_cache_from_tree,
    _render_scan_header,
    _scan_status_message,
    _scan_with_spinner,
    _sudo_remove,
    build_analysis_entry,
    get_rust_scan_data,
    get_rust_tree_data,
    run_deep_analysis,
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
    # Homes the cursor and clears the screen.
    assert output.startswith("\033[H\033[J")
    assert "Analyze Disk" in output
    # The title must sit on row 2 (one blank line above it), matching
    # AnalyzeSelector.render(), so the screen does not shift vertically when
    # the scan screen hands off to the result list.
    after_home = output.split("\033[H")[-1]
    assert after_home[: after_home.index("Analyze Disk")].count("\n") == 1


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._get_rust_scan_data_with_spinner")
@patch("src.core.analyze._tree_scan_with_spinner")
def test_drill_into_cached_dir_skips_scan(mock_tree, mock_single, mock_selector, test_env):
    """Drilling into an already-cached directory must not run any scan (so the
    view redraws in place with no flash or jitter)."""
    ScanCache.clear()
    dir_a = test_env / "A"
    dir_b = dir_a / "B"
    dir_b.mkdir(parents=True)
    ScanCache.set(dir_a, {"total_size_bytes": 1000, "subdirs": {"B": 500}})
    ScanCache.set(dir_b, {"total_size_bytes": 500, "subdirs": {}})

    # Drill into the only entry (B), then quit.
    mock_selector.return_value.run.side_effect = [("DRILL_DOWN", 0), ("QUIT", None)]

    run_deep_analysis(dir_a)

    mock_tree.assert_not_called()
    mock_single.assert_not_called()


@patch("subprocess.run")
def test_get_rust_tree_data_parses(mock_run):
    """--tree output parses into a per-directory map and does not touch the cache."""
    ScanCache.clear()
    tree = {
        ".": {"total_size_bytes": 1000, "file_count": 3, "subdirs": {"docs": 1000}},
        "docs": {"total_size_bytes": 1000, "file_count": 3, "subdirs": {"a.bin": 1000}},
    }
    mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(tree))

    with patch("pathlib.Path.exists", return_value=True):
        result = get_rust_tree_data(Path("/some/dir"))

    assert result == tree
    # The bulk scan itself must not populate the cache; priming is a separate step.
    assert ScanCache.get(Path("/some/dir")) is None


def test_prime_cache_from_tree_keys(test_env):
    """Tree keys are rejoined onto the original root so they match parent/name lookups."""
    ScanCache.clear()
    root = test_env / "proj"
    tree = {
        ".": {"total_size_bytes": 30, "file_count": 3, "subdirs": {"docs": 20, "x.bin": 10}},
        "docs": {"total_size_bytes": 20, "file_count": 2, "subdirs": {"sub": 20}},
        "docs/sub": {"total_size_bytes": 20, "file_count": 2, "subdirs": {"y.bin": 20}},
    }

    _prime_cache_from_tree(root, tree)

    assert ScanCache.get(root)["total_size_bytes"] == 30
    assert ScanCache.get(root / "docs")["subdirs"]["sub"] == 20
    assert ScanCache.get(root / "docs" / "sub")["total_size_bytes"] == 20


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._get_rust_scan_data_with_spinner")
@patch("src.core.analyze._tree_scan_with_spinner")
def test_drill_after_tree_scan_hits_cache(mock_tree, mock_single, mock_selector, test_env):
    """Entering a directory tree-scans once and primes every level; drilling into
    a child is then a pure cache hit — no second scan."""
    ScanCache.clear()
    dir_a = test_env / "A"
    (dir_a / "B").mkdir(parents=True)
    mock_tree.return_value = {
        ".": {"total_size_bytes": 1000, "file_count": 2, "subdirs": {"B": 800, "f.txt": 200}},
        "B": {"total_size_bytes": 800, "file_count": 1, "subdirs": {"g.txt": 800}},
    }
    mock_selector.return_value.run.side_effect = [("DRILL_DOWN", 0), ("QUIT", None)]

    run_deep_analysis(dir_a)

    # Whole-subtree scan happens exactly once, on the initial entry.
    mock_tree.assert_called_once()
    # The drill into B is served from the primed cache; no single-level fallback.
    mock_single.assert_not_called()


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._get_rust_scan_data_with_spinner")
@patch("src.core.analyze._tree_scan_with_spinner")
def test_tree_scan_failure_falls_back_to_single(mock_tree, mock_single, mock_selector, test_env):
    """If the engine can't do a tree scan (e.g. an older binary), drilling falls
    back to a single-level scan so the feature degrades gracefully."""
    ScanCache.clear()
    dir_a = test_env / "A"
    dir_a.mkdir()
    mock_tree.return_value = None  # old / failed engine
    mock_single.return_value = {"total_size_bytes": 500, "subdirs": {}, "top_files": []}
    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis(dir_a)

    mock_tree.assert_called_once()
    mock_single.assert_called_once()


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._parallel_scan_sizes")
@patch("src.core.analyze._tree_scan_with_spinner")
@patch("src.core.analyze._get_rust_scan_data_with_spinner")
def test_root_view_uses_single_scan_not_tree(
    mock_single, mock_tree, mock_parallel, mock_selector, test_env
):
    """The root category view must NOT trigger a whole-subtree tree scan."""
    ScanCache.clear()
    mock_single.return_value = {"total_size_bytes": 1000, "subdirs": {}, "top_files": []}
    mock_parallel.return_value = {}
    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis()  # no target_path -> root view (current_target is None)

    mock_tree.assert_not_called()
    mock_single.assert_called_once()


@patch("src.core.analyze.SCAN_SPINNER_DELAY", 5.0)
@patch("src.core.analyze._render_scan_header")
def test_scan_with_spinner_skips_header_for_fast_scan(mock_header):
    """A scan that finishes within the grace period never paints the scan screen,
    so fast small-dir scans hand off to the list with an in-place redraw."""
    result = _scan_with_spinner(lambda: {"total_size_bytes": 1}, "scan", "X", "Title")

    assert result == {"total_size_bytes": 1}
    mock_header.assert_not_called()


@patch("src.core.analyze.SCAN_SPINNER_DELAY", 0.0)
@patch("src.core.analyze._render_scan_header")
def test_scan_with_spinner_shows_header_for_slow_scan(mock_header):
    """A scan slower than the grace period paints the scan screen + spinner."""

    def slow():
        time.sleep(0.12)
        return {"total_size_bytes": 1}

    result = _scan_with_spinner(slow, "scan", "X", "Title")

    assert result == {"total_size_bytes": 1}
    mock_header.assert_called()


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
        patch("src.core.analyze._ensure_admin_for_delete", return_value=True) as mock_admin_check,
        patch("src.core.analyze.safe_remove", return_value=(True, "Moved to trash")) as mock_safe,
        patch("src.core.analyze._sudo_remove") as mock_sudo,
    ):
        assert _delete_analyze_paths([target]) is True

    mock_admin_check.assert_called_once_with([target])
    mock_safe.assert_called_once_with(target, use_trash=True)
    mock_sudo.assert_not_called()


def test_analyze_delete_system_path_requires_admin():
    target = Path("/var/cache/topo-test")

    with (
        patch("src.core.analyze.get_size", return_value=4096),
        patch("src.core.analyze._ensure_admin_for_delete", return_value=True) as mock_admin_check,
        patch("src.core.analyze.safe_remove") as mock_safe,
        patch("src.core.analyze._sudo_remove", return_value=True) as mock_sudo,
    ):
        assert _delete_analyze_paths([target]) is True

    mock_admin_check.assert_called_once_with([target])
    mock_sudo.assert_called_once_with(target)
    mock_safe.assert_not_called()


def test_needs_admin_for_deletion_rejects_non_home_path():
    assert _needs_admin_for_deletion(Path("/usr/share/topo-test")) is True


def test_sudo_remove_operates_on_resolved_path(test_env):
    """Regression (M1): the path validated must be the exact path handed to
    `rm -rf`. Operate on the resolved path so a symlinked component cannot make
    validation and deletion disagree (validate target A, delete target B)."""
    real_dir = test_env / "real-target"
    real_dir.mkdir()
    link = test_env / "link-to-target"
    link.symlink_to(real_dir)

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(ok=True)

    with (
        patch("src.core.analyze.run_command", side_effect=fake_run),
        patch("src.core.analyze.get_size", return_value=0),
    ):
        assert _sudo_remove(link) is True

    # rm must target the resolved real directory, never the raw symlink path.
    assert captured["cmd"] == ["rm", "-rf", "--", str(real_dir.resolve())]
