import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from functools import cache
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.analyze import (
    DELETE_CANCELLED_PROBLEM,
    PERMANENT_DELETE_QUESTION,
    DeleteOutcome,
    _delete_analyze_paths,
    _needs_admin_for_deletion,
    _permanent_fallback_consent,
    _sudo_remove,
    build_analysis_entry,
    build_linux_insights,
    filesystem_used_bytes,
    get_fast_explore_data,
    get_old_items_info,
    get_rust_scan_data,
    get_rust_tree_data,
    normalize_scan_path,
    parallel_scan_sizes,
    percent_of,
)
from src.core.file_ops import CACHEDIR_TAG_SIGNATURE, has_valid_cachedir_tag
from src.core.scan_cache import ScanCache
from src.ui.navigator import ANSI_CSI_RE
from src.ui.screens.analyze import (
    ENGINE_SCAN_FAILED_NOTICE,
    NOTICE_TEXT_LIMIT,
    XDG_OPEN_MISSING_NOTICE,
    _confirm_permanent_delete,
    _delete_notice,
    _explore_notice,
    _fail_notice,
    _open_in_file_manager,
    _render_scan_header,
    _scan_status_message,
    _scan_with_spinner,
    _warn_notice,
    run_deep_analysis,
)


def test_scan_cache():
    """Verify that ScanCache stores and retrieves data correctly."""
    path = Path("/tmp/test_path")
    data = {"total_size_bytes": 1024}

    ScanCache.set(path, data)
    assert ScanCache.get(path) == data

    # Check that a different path returns None
    assert ScanCache.get(Path("/tmp/other")) is None


def test_scan_cache_evicts_least_recently_used_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(ScanCache, "MAX_ENTRIES", 2)
    first, second, third = (tmp_path / name for name in ("first", "second", "third"))
    for path in (first, second, third):
        path.mkdir()

    ScanCache.clear()
    ScanCache.set(first, {"total_size_bytes": 1})
    ScanCache.set(second, {"total_size_bytes": 2})
    assert ScanCache.get(first) is not None  # first is now most recently used
    ScanCache.set(third, {"total_size_bytes": 3})

    assert ScanCache.get(second) is None
    assert ScanCache.get(first) is not None
    assert ScanCache.get(third) is not None


def test_scan_cache_invalidates_changed_directory(tmp_path):
    directory = tmp_path / "cached"
    directory.mkdir()
    ScanCache.clear()
    ScanCache.set(directory, {"total_size_bytes": 1})

    (directory / "new-file").write_text("changed")

    assert ScanCache.get(directory) is None


def test_scan_cache_rejects_single_entry_over_memory_limit(monkeypatch, tmp_path):
    directory = tmp_path / "huge"
    directory.mkdir()
    monkeypatch.setattr(ScanCache, "MAX_ESTIMATED_BYTES", 100)
    ScanCache.clear()

    ScanCache.set(directory, {"subdirs": {"x" * 200: 1}})

    assert ScanCache.get(directory) is None


def test_scan_cache_uses_rust_estimate_without_rewalking_data(tmp_path):
    directory = tmp_path / "hinted"
    directory.mkdir()
    data = {
        "total_size_bytes": 10,
        "subdirs": {f"item-{index}": index for index in range(100)},
        "_cache_estimated_bytes": 4096,
    }

    with patch.object(ScanCache, "_estimate", wraps=ScanCache._estimate) as estimate:
        ScanCache.set(directory, data)

    estimate.assert_not_called()
    assert ScanCache._data[str(directory)].estimated_bytes == 4096


@pytest.mark.parametrize("invalid_hint", ["invalid", 0, -1, None, True, False, 4096.0])
def test_scan_cache_invalid_rust_estimate_falls_back(tmp_path, invalid_hint):
    directory = tmp_path / "invalid-hint"
    directory.mkdir()
    data = {"total_size_bytes": 10, "_cache_estimated_bytes": invalid_hint}

    with patch.object(ScanCache, "_estimate", wraps=ScanCache._estimate) as estimate:
        ScanCache.set(directory, data)

    estimate.assert_called_once_with(data)


@cache
def _dev_engine_binary() -> Path:
    """Build and return the engine from the current checkout.

    Deliberately not ``get_core_binary()``: that returns the *packaged* binary
    under ``src/core/bin``, which is a release artifact and can predate the
    scanner source being tested here.
    """
    project_root = Path(__file__).resolve().parent.parent
    manifest = project_root / "topo-core" / "Cargo.toml"
    subprocess.run(
        ["cargo", "build", "--quiet", "--manifest-path", str(manifest)],
        cwd=project_root,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    binary = project_root / "topo-core" / "target" / "debug" / "topo-core"
    assert binary.is_file(), f"cargo build did not produce {binary}"
    return binary


def test_rust_cache_hint_never_undercharges_the_generic_estimate(tmp_path):
    """Pin the two estimators together across the language boundary.

    The hint lets ``set()`` skip ``_estimate`` entirely, so the 64 MiB budget is
    only as sound as the hint. Rust must therefore charge at least what the
    generic estimator would for the very same dict -- including the absolute
    ``path`` that ``get_rust_tree_data`` adds to every ``--tree`` node, which
    the aggregate itself does not carry.
    """
    binary = _dev_engine_binary()
    for index in range(30):
        child = tmp_path / f"child-{index:03d}-with-a-longish-name" / "nested" / "deeper"
        child.mkdir(parents=True)
        (child / "f.bin").write_bytes(b"x" * 512)
    (tmp_path / "目录-中文" / "inner").mkdir(parents=True)
    (tmp_path / "目录-中文" / "inner" / "f.bin").write_bytes(b"y" * 512)

    root = normalize_scan_path(tmp_path)
    single = json.loads(
        subprocess.run([str(binary), str(root)], capture_output=True, text=True, check=True).stdout
    )
    assert single["_cache_estimated_bytes"] >= ScanCache._estimate(single)

    tree = json.loads(
        subprocess.run(
            [str(binary), "--tree", str(root), "--min-bytes", "0"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    assert len(tree) > 30
    for relative, aggregate in tree.items():
        node = root if relative == "." else root / relative
        cached = {"path": str(node), "top_files": [], **aggregate}
        hint = aggregate["_cache_estimated_bytes"]
        assert hint >= ScanCache._estimate(cached), f"{relative} undercharged by {hint}"


def test_scan_cache_byte_accounting_stays_consistent_under_concurrency(monkeypatch, tmp_path):
    """Concurrent set/get from many threads must not corrupt the cache.

    ``parallel_scan_sizes`` seeds the cache from a ThreadPoolExecutor (several
    workers each calling ``set`` many times). The byte counter and LRU order are
    read-modify-write state; without the class-level lock, concurrent eviction
    both drifts ``_estimated_bytes`` and races ``move_to_end`` into a KeyError.
    A tiny switch interval forces the preemption that makes the regression
    deterministic; a low entry cap keeps eviction firing throughout.
    """
    monkeypatch.setattr(ScanCache, "MAX_ENTRIES", 8)
    ScanCache.clear()

    dirs = []
    for i in range(24):
        directory = tmp_path / f"d{i}"
        directory.mkdir()
        dirs.append(directory)

    barrier = threading.Barrier(6)
    errors: list[Exception] = []

    def worker() -> None:
        try:
            barrier.wait()
            for _ in range(150):
                for directory in dirs:
                    ScanCache.set(directory, {"total_size_bytes": 1, "subdirs": {"a": 1, "b": 2}})
                    ScanCache.get(directory)
        except Exception as exc:  # noqa: BLE001 - surface any thread failure to the assertion
            errors.append(exc)

    previous_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)  # force frequent preemption to expose any missing lock
    try:
        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        sys.setswitchinterval(previous_interval)

    assert not errors
    # The running counter must exactly equal the live entries' total; a lost
    # +=/-= update under a race would break this equality.
    assert ScanCache._estimated_bytes == sum(
        entry.estimated_bytes for entry in ScanCache._data.values()
    )
    assert len(ScanCache._data) <= ScanCache.MAX_ENTRIES
    assert ScanCache._estimated_bytes >= 0
    ScanCache.clear()


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


def test_get_rust_tree_data_seeds_descendant_cache():
    root = Path("/home/user")
    payload = {
        ".": {"total_size_bytes": 30, "file_count": 2, "subdirs": {"cache": 20}},
        "cache": {"total_size_bytes": 20, "file_count": 1, "subdirs": {"x": 20}},
    }
    ScanCache.clear()
    with (
        patch("src.core.engine.get_core_binary", return_value=Path("/tmp/topo-core")),
        patch(
            "src.core.engine.run_command",
            return_value=MagicMock(ok=True, stdout=json.dumps(payload)),
        ),
    ):
        data = get_rust_tree_data(root)

    assert data["total_size_bytes"] == 30
    assert ScanCache.get(root / "cache")["total_size_bytes"] == 20


def test_parallel_scan_sizes_collapses_nested_roots_and_limits_workers():
    ScanCache.clear()
    root = Path("/home/user")
    child = root / ".cache/models"
    other = Path("/usr")
    scanned = []

    def fake_tree(path):
        scanned.append(path)
        ScanCache.set(path, {"total_size_bytes": 100})
        if path == root:
            ScanCache.set(child, {"total_size_bytes": 25})

    with patch("src.analyze.get_rust_tree_data", side_effect=fake_tree):
        sizes = parallel_scan_sizes([root, child, other, child])

    assert scanned == [root, other]
    assert sizes == {root: 100, child: 25, other: 100}


def test_parallel_scan_sizes_notifies_only_when_scanning():
    root = Path("/home/user")
    notice = MagicMock()
    ScanCache.clear()

    def fake_tree(path):
        ScanCache.set(path, {"total_size_bytes": 100})

    with patch("src.analyze.get_rust_tree_data", side_effect=fake_tree):
        assert parallel_scan_sizes([root], on_scan_start=notice) == {root: 100}
        assert parallel_scan_sizes([root], on_scan_start=notice) == {root: 100}

    notice.assert_called_once_with()


def test_parallel_scan_sizes_notifies_once_across_both_scan_phases():
    """A single call can run BOTH phases: the tree-scan of the roots and then the
    get_rust_scan_data sweep of whatever the tree-scan left uncached. The
    "Analyzing..." notice must still fire exactly once, not once per phase."""
    a = Path("/data/a")
    b = Path("/data/b")  # non-nested sibling, so both start as roots
    notice = MagicMock()
    ScanCache.clear()

    def tree_seeds_only_a(path):
        # The roots tree-scan resolves A but not B, leaving B in `missing`
        # so the second (get_rust_scan_data) phase also runs.
        if path == a:
            ScanCache.set(path, {"total_size_bytes": 10})

    def scan_seeds_b(path):
        ScanCache.set(path, {"total_size_bytes": 20})

    with (
        patch("src.analyze.get_rust_tree_data", side_effect=tree_seeds_only_a),
        patch("src.analyze.get_rust_scan_data", side_effect=scan_seeds_b),
    ):
        sizes = parallel_scan_sizes([a, b], on_scan_start=notice)

    assert sizes == {a: 10, b: 20}
    notice.assert_called_once_with()
    ScanCache.clear()


def test_parallel_scan_sizes_reuses_pre_seeded_descendants():
    """Descendants already seeded in ScanCache (e.g. by the root-view Home tree
    scan) must not be tree-scanned a second time here."""
    home = Path("/home/user")
    hub = home / ".cache/huggingface/hub"
    models = home / ".ollama/models"
    other = Path("/usr")
    # Simulate the root-view Home tree scan having already seeded large descendants.
    ScanCache.clear()
    ScanCache.set(hub, {"total_size_bytes": 500})
    ScanCache.set(models, {"total_size_bytes": 300})
    scanned = []

    def fake_tree(path):
        scanned.append(path)
        ScanCache.set(path, {"total_size_bytes": 100})

    with patch("src.analyze.get_rust_tree_data", side_effect=fake_tree):
        sizes = parallel_scan_sizes([other, hub, models])

    # hub and models are cache hits -> only /usr is scanned.
    assert scanned == [other]
    assert sizes == {other: 100, hub: 500, models: 300}


def test_parallel_scan_sizes_matches_symlinked_input_to_resolved_cache_key(tmp_path):
    """The scan functions key the cache by the normalized (symlink-resolved)
    path, so parallel_scan_sizes must probe under the same key. A symlinked
    input (e.g. ~/.cache moved to another partition) would otherwise be seeded
    under its resolved key but read under the raw key, silently reporting 0."""
    ScanCache.clear()
    real = tmp_path / "real_cache"
    real.mkdir()
    link = tmp_path / "linked_cache"
    link.symlink_to(real, target_is_directory=True)
    assert normalize_scan_path(link) != link  # guard: the symlink must resolve

    def fake_tree(path):
        # Mimic get_rust_tree_data: cache under the normalized (resolved) key.
        ScanCache.set(normalize_scan_path(path), {"total_size_bytes": 4096})

    with patch("src.analyze.get_rust_tree_data", side_effect=fake_tree):
        sizes = parallel_scan_sizes([link])

    # Keyed by the caller's original path, with the size found via the resolved key.
    assert sizes == {link: 4096}


def test_get_rust_tree_data_survives_lru_eviction(test_env):
    """The tree result must return its root even if cache capacity is exceeded."""
    from src.analyze import get_rust_tree_data

    large_tree = {
        ".": {"total_size_bytes": 1000, "file_count": 10, "subdirs": {}},
    }
    for i in range(12):
        large_tree[f"sub_{i}"] = {"total_size_bytes": 1, "file_count": 1, "subdirs": {}}

    fake_res = MagicMock()
    fake_res.ok = True
    fake_res.stdout = json.dumps(large_tree)

    with (
        patch("src.core.engine.run_command", return_value=fake_res),
        patch.object(ScanCache, "MAX_ENTRIES", 4),
    ):
        result = get_rust_tree_data(test_env)

    assert result is not None
    assert result["total_size_bytes"] == 1000
    assert ScanCache.get(test_env.resolve()) is result
    assert next(reversed(ScanCache._data)) == str(test_env.resolve())


def test_get_rust_tree_data_returns_root_even_when_root_is_too_large_to_cache(test_env):
    root_data = {
        "total_size_bytes": 1000,
        "file_count": 10,
        "subdirs": {"oversized-name": 1000},
    }
    fake_res = MagicMock(ok=True, stdout=json.dumps({".": root_data}))
    with (
        patch("src.core.engine.run_command", return_value=fake_res),
        patch.object(ScanCache, "MAX_ESTIMATED_BYTES", 1),
    ):
        result = get_rust_tree_data(test_env)

    assert result is not None
    assert result["total_size_bytes"] == 1000
    assert ScanCache.get(test_env.resolve()) is None


def test_normalize_scan_path_falls_back_when_resolve_fails(tmp_path):
    relative = Path("relative-target")
    with patch.object(Path, "resolve", side_effect=OSError("unavailable")):
        normalized = normalize_scan_path(relative)

    assert normalized == relative.absolute()


def test_has_valid_cachedir_tag(test_env):
    cache_dir = test_env / "cache-dir"
    cache_dir.mkdir()
    (cache_dir / "CACHEDIR.TAG").write_text(f"{CACHEDIR_TAG_SIGNATURE}\nextra metadata")

    assert has_valid_cachedir_tag(cache_dir) is True


def test_scan_status_message_uses_spinner_frame():
    scan_msg = _scan_status_message("scan", "Home", "⠋")
    refresh_msg = _scan_status_message("refresh", "Downloads", "⠙")

    assert "⠋" in scan_msg and "Rust Engine: Analyzing disk usage" in scan_msg
    assert "⠙" in refresh_msg and "Refreshing analysis on Downloads" in refresh_msg
    assert scan_msg.startswith(" ") and not scan_msg.startswith("   ")
    assert "🚀" not in scan_msg


def test_render_scan_header_repaints_in_place_without_full_clear(capsys):
    # ERASE_BELOW is empty unless stdout is a terminal, and under pytest it never
    # is; restore it so this test sees the sequence a real terminal gets.
    with patch("src.ui.screens.analyze.ERASE_BELOW", "\033[J"):
        _render_scan_header("Analyze Disk")

    output = capsys.readouterr().out
    # Repaints in place: homes the cursor and clears row by row, but must NOT
    # issue a full-screen CLEAR_SCREEN (\033[2J) -- that blanks the whole screen
    # in a discrete step and flashes the previous list to black when a sub-view
    # scan just barely crosses the spinner grace period.
    assert output.startswith("\033[H")
    assert "\033[2J" not in output
    # Still erases below so the previous frame's body does not show through.
    assert "\033[J" in output
    assert "Analyze Disk" in output
    # The title must sit on row 2 (one blank line above it), matching
    # AnalyzeSelector.render(), so the screen does not shift vertically when
    # the scan screen hands off to the result list.
    after_home = output.split("\033[H")[-1]
    assert after_home[: after_home.index("Analyze Disk")].count("\n") == 1


@patch("src.ui.screens.analyze.AnalyzeSelector")
@patch("src.ui.screens.analyze.FAST_EXPLORE_ENTRY_LIMIT", 1)
@patch("src.ui.screens.analyze._get_rust_scan_data_with_spinner")
def test_fast_explore_ignores_rust_scan_cache(mock_single, mock_selector, test_env):
    """Wide-directory preview should show the live direct listing, not stale Rust cache data."""
    ScanCache.clear()
    dir_a = test_env / "A"
    (dir_a / "B").mkdir(parents=True)
    (dir_a / "fresh.txt").write_bytes(b"fresh")
    ScanCache.set(dir_a, {"total_size_bytes": 1000, "subdirs": {"stale-only": 1000}})

    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis(dir_a)

    mock_single.assert_not_called()
    shown_names = {item["name"] for item in mock_selector.call_args.args[1]}
    assert "stale-only" not in shown_names
    assert shown_names and shown_names <= {"B", "fresh.txt"}


def test_fast_explore_data_only_previews_wide_directories(test_env):
    """One pass answers both questions: a narrow directory returns None so the
    caller runs the full scan, without a second traversal to count entries."""
    root = test_env / "wide"
    root.mkdir()
    for index in range(3):
        (root / f"item-{index}").write_text("x")

    assert get_fast_explore_data(root, 2, only_when_wide=True) is not None
    assert get_fast_explore_data(root, 3, only_when_wide=True) is None
    # Without the flag the listing is always built -- that is the engine-failure
    # fallback, which has no full scan to defer to.
    assert get_fast_explore_data(root, 3, only_when_wide=False) is not None


class _NoStatScandir:
    """Stand-in for os.scandir whose entries refuse to be stat'd."""

    class _Entry:
        def __init__(self, name: str) -> None:
            self.name = name

        def is_dir(self, follow_symlinks: bool = True) -> bool:
            return False

        def stat(self, follow_symlinks: bool = True):
            raise AssertionError("a narrow directory must not stat its entries")

    def __init__(self, names: list[str]) -> None:
        self._entries = [self._Entry(name) for name in names]

    def __enter__(self):
        return iter(self._entries)

    def __exit__(self, *_exc_info) -> bool:
        return False


def test_narrow_directory_costs_no_per_entry_stat(test_env):
    """A directory that turns out to be narrow must not pay for the sample it
    never returns: names are collected first and stat'd only once wide."""
    with patch("src.analyze.os.scandir", return_value=_NoStatScandir(["only"])):
        assert get_fast_explore_data(test_env, 5, only_when_wide=True) is None


def test_fast_explore_data_is_direct_listing_and_non_recursive(test_env):
    ScanCache.clear()
    root = test_env / "Explore"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (root / "a").write_bytes(b"a")
    (root / "b").write_bytes(b"bb")
    (nested / "inner").write_bytes(b"inner-data")

    data = get_fast_explore_data(root, entry_limit=10)

    assert data is not None
    assert data["is_fast_explore"] is True
    assert data["preview_truncated"] is False
    assert data["preview_sampled_entries"] == 3
    assert data["total_size_bytes"] == 3
    assert data["file_count"] == 2
    assert data["subdirs"]["nested"] == 0
    assert data["entry_meta"]["nested"]["is_dir"] is True
    assert data["entry_meta"]["nested"]["size_known"] is False
    assert data["entry_meta"]["a"]["size_known"] is True
    assert "inner" not in data["subdirs"]


def test_fast_explore_data_stops_at_entry_limit(test_env):
    ScanCache.clear()
    root = test_env / "wide"
    root.mkdir()
    for index in range(3):
        (root / f"item-{index}").write_text("x")

    data = get_fast_explore_data(root, entry_limit=2)

    assert data is not None
    assert data["preview_truncated"] is True
    assert data["preview_sampled_entries"] == 2
    assert len(data["subdirs"]) == 2


def test_fast_explore_notice_explains_truncation():
    notice = _explore_notice(
        {
            "is_fast_explore": True,
            "preview_entry_limit": 500,
            "preview_sampled_entries": 500,
            "preview_truncated": True,
            "subdirs": {"a": 1, "b": 2},
        }
    )

    assert "Preview mode" in notice
    assert "showing first 500 direct entries" in notice
    assert "folder sizes are not calculated" in notice


@patch("src.ui.screens.analyze.AnalyzeSelector")
@patch("src.ui.screens.analyze._get_rust_scan_data_with_spinner")
def test_regular_directory_uses_rust_size_view(mock_single, mock_selector, test_env):
    ScanCache.clear()
    dir_a = test_env / "A"
    (dir_a / "B").mkdir(parents=True)
    (dir_a / "a.txt").write_bytes(b"abc")
    mock_single.return_value = {
        "total_size_bytes": 1000,
        "subdirs": {"B": 997, "a.txt": 3},
        "top_files": [],
    }
    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis(dir_a)

    mock_single.assert_called_once()
    assert mock_selector.call_args.kwargs["sort_mode"] == "size"
    assert mock_selector.call_args.kwargs["notice"] == ""
    by_name = {item["name"]: item for item in mock_selector.call_args.args[1]}
    assert by_name["B"]["size"] == 997
    assert by_name["a.txt"]["size"] == 3


@patch("src.ui.screens.analyze.AnalyzeSelector")
@patch("src.ui.screens.analyze.FAST_EXPLORE_ENTRY_LIMIT", 1)
@patch("src.ui.screens.analyze._get_rust_scan_data_with_spinner")
@patch("src.ui.screens.analyze.build_analysis_entry")
def test_fast_explore_builds_rows_without_per_path_analysis(
    mock_build_entry, mock_single, mock_selector, test_env
):
    ScanCache.clear()
    directory = test_env / "many"
    (directory / "nested").mkdir(parents=True)
    (directory / "item.txt").write_bytes(b"x")
    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis(directory)

    mock_single.assert_not_called()
    mock_build_entry.assert_not_called()


@patch("src.ui.screens.analyze.AnalyzeSelector")
@patch("src.ui.screens.analyze.FAST_EXPLORE_ENTRY_LIMIT", 2)
@patch("src.ui.screens.analyze._get_rust_scan_data_with_spinner")
def test_wide_cache_directory_uses_fast_explore_not_rust(mock_single, mock_selector, test_env):
    ScanCache.clear()
    icon_cache = test_env / ".cache/gnome-software/icons"
    icon_cache.mkdir(parents=True)
    for index in range(3):
        (icon_cache / f"icon-{index}.png").write_bytes(b"x")
    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis(icon_cache)

    mock_single.assert_not_called()
    shown_names = {item["name"] for item in mock_selector.call_args.args[1]}
    # Which two of the three land in the sample depends on readdir order; what
    # matters is that the rows came from the live listing.
    assert len(shown_names) == 2
    assert shown_names <= {f"icon-{index}.png" for index in range(3)}


@patch("src.ui.screens.analyze.AnalyzeSelector")
@patch("src.ui.screens.analyze.parallel_scan_sizes")
@patch("src.ui.screens.analyze.get_rust_tree_data")
def test_root_view_uses_tree_scan(mock_tree, mock_parallel, mock_selector, test_env):
    ScanCache.clear()
    mock_tree.return_value = {"total_size_bytes": 1000, "subdirs": {}, "top_files": []}
    mock_parallel.return_value = {}
    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis()  # no target_path -> root view (current_target is None)

    mock_tree.assert_called_once_with(test_env)


@patch("src.ui.screens.analyze.AnalyzeSelector")
@patch("src.ui.screens.analyze.parallel_scan_sizes")
@patch("src.ui.screens.analyze.get_rust_tree_data")
def test_root_view_repins_home_after_secondary_cache_churn(
    mock_tree, mock_parallel, mock_selector, test_env
):
    ScanCache.clear()
    root_data = {"total_size_bytes": 1000, "subdirs": {}, "top_files": []}

    def scan_home(path):
        ScanCache.set(path, root_data)
        return root_data

    def churn_cache(_paths, **_kwargs):
        for index in range(5):
            ScanCache.set(test_env / f"secondary-{index}", {"total_size_bytes": index + 1})
        return {}

    mock_tree.side_effect = scan_home
    mock_parallel.side_effect = churn_cache
    mock_selector.return_value.run.side_effect = [("QUIT", None), ("QUIT", None)]

    with patch.object(ScanCache, "MAX_ENTRIES", 4):
        run_deep_analysis()
        run_deep_analysis()

    mock_tree.assert_called_once_with(test_env)
    assert ScanCache.get(test_env) is root_data
    ScanCache.clear()


@patch("src.ui.screens.analyze.AnalyzeSelector")
@patch("src.ui.screens.analyze.parallel_scan_sizes")
@patch("src.ui.screens.analyze.get_fast_explore_data")
@patch("src.ui.screens.analyze.get_rust_tree_data")
def test_root_view_does_not_pin_a_fast_explore_fallback_as_home(
    mock_tree, mock_fast, mock_parallel, mock_selector, test_env
):
    """When the Home tree scan yields nothing and the view falls back to a fast
    preview, that partial listing must not be pinned under the Home key. The
    re-pin exists to keep a *full* scan hot; caching a preview there would serve
    it as the full scan on the next entry."""
    ScanCache.clear()
    mock_tree.return_value = {}  # engine came back empty -> fast-explore fallback
    mock_fast.return_value = {
        "total_size_bytes": 42,
        "subdirs": {},
        "top_files": [],
        "is_fast_explore": True,
    }
    mock_parallel.return_value = {}
    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis()

    assert ScanCache.get(test_env) is None
    ScanCache.clear()


@patch("src.ui.screens.analyze.SCAN_SPINNER_DELAY", 5.0)
@patch("src.ui.screens.analyze._render_scan_header")
def test_scan_with_spinner_skips_header_for_fast_scan(mock_header):
    """A scan that finishes within the grace period never paints the scan screen,
    so fast small-dir scans hand off to the list with an in-place redraw."""
    result = _scan_with_spinner(lambda: {"total_size_bytes": 1}, "scan", "X", "Title")

    assert result == {"total_size_bytes": 1}
    mock_header.assert_not_called()


@patch("src.ui.screens.analyze.SCAN_SPINNER_DELAY", 0.0)
@patch("src.ui.screens.analyze._render_scan_header")
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
    assert entry["icon"] == "📁"
    assert entry["percent"] == 50


def test_build_analysis_entry_marks_browser_cache_as_cleanable(test_env):
    cache_dir = test_env / ".mozilla/firefox/profile.default/cache2"
    cache_dir.mkdir(parents=True)

    entry = build_analysis_entry("Cache", cache_dir, size=512, total_size=1024)

    assert entry["is_cleanable"] is True
    assert entry["cleanable_reason"] == "App cache"
    assert entry["icon"] == "📁"


def test_build_analysis_entry_marks_desktop_app_cache_as_cleanable(test_env):
    cache_dir = test_env / ".cache/spotify/Data"
    cache_dir.mkdir(parents=True)

    entry = build_analysis_entry("Data", cache_dir, size=512, total_size=1024)

    assert entry["is_cleanable"] is True
    assert entry["cleanable_reason"] == "App cache"
    assert entry["icon"] == "📁"


def test_build_analysis_entry_marks_generic_xdg_cache_as_cleanable(test_env):
    cache_dir = test_env / ".cache/random-tool"
    cache_dir.mkdir(parents=True)

    entry = build_analysis_entry("random-tool", cache_dir, size=512, total_size=1024)
    root_entry = build_analysis_entry(".cache", test_env / ".cache", size=512, total_size=1024)

    assert entry["is_cleanable"] is True
    assert entry["cleanable_reason"] == "XDG cache"
    assert entry["icon"] == "📁"
    assert root_entry["is_cleanable"] is False


def test_build_linux_insights_uses_shared_heavy_cache_metadata(test_env):
    insights = build_linux_insights(test_env)
    by_name = {item["name"]: item for item in insights}

    assert by_name["Apt Cache"]["path"] == Path("/var/cache/apt/archives")
    assert "Dnf Cache" not in by_name
    assert by_name["Docker System"]["path"] == Path("/var/lib/docker")
    assert by_name["Podman Transfer Cache"]["path"] == test_env / ".cache/containers"
    assert "Podman Storage" not in by_name
    assert by_name["HuggingFace Hub"]["path"] == test_env / ".cache/huggingface/hub"
    assert by_name["LM Studio Cache"]["path"] == test_env / ".cache/lm-studio"
    assert by_name["Old Downloads (90d+)"]["is_smart"] is True


def test_analyze_delete_user_writable_path_without_admin(test_env):
    target = test_env / "owned-file.txt"
    target.write_text("remove me")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.analyze._ensure_admin_for_delete", return_value="") as mock_admin_check,
        patch("src.analyze.safe_remove", return_value=(True, "Moved to trash")) as mock_safe,
        patch("src.analyze._sudo_remove") as mock_sudo,
    ):
        outcome = _delete_analyze_paths([target])

    # The batch reports numbers instead of printing: Analyze repaints its frame
    # as soon as this returns, so anything printed here would be overwritten.
    assert (outcome.deleted, outcome.failed, outcome.first_problem) == (1, 0, "")
    assert outcome.freed_bytes == target.stat().st_size
    mock_admin_check.assert_called_once_with([target])
    mock_safe.assert_called_once_with(target, use_trash=True)
    mock_sudo.assert_not_called()


def test_analyze_delete_browser_profile_root_cleans_cache_children(test_env):
    chrome_root = test_env / ".config/google-chrome"
    chrome_profile_dir = chrome_root / "Default"
    chrome_cache_dir = chrome_profile_dir / "Cache"
    chrome_code_cache_dir = chrome_profile_dir / "Code Cache"
    chrome_login_db = chrome_profile_dir / "Login Data"
    chrome_cache_dir.mkdir(parents=True)
    chrome_code_cache_dir.mkdir()
    (chrome_cache_dir / "data.bin").write_text("cache")
    (chrome_code_cache_dir / "script.bin").write_text("cache")
    chrome_login_db.write_text("{}")

    firefox_root = test_env / ".mozilla"
    firefox_profile_dir = firefox_root / "firefox/profile.default"
    firefox_cache_dir = firefox_profile_dir / "cache2"
    firefox_startup_cache_dir = firefox_profile_dir / "startupCache"
    firefox_login_db = firefox_profile_dir / "logins.json"
    firefox_cache_dir.mkdir(parents=True)
    firefox_startup_cache_dir.mkdir()
    (firefox_cache_dir / "entry.bin").write_text("cache")
    (firefox_startup_cache_dir / "startup.bin").write_text("cache")
    firefox_login_db.write_text("{}")

    with (
        patch("src.analyze._ensure_admin_for_delete", return_value=""),
        # _which_cached() memoizes, so patching shutil.which alone is order-dependent.
        patch("src.core.file_ops._which_cached", return_value=None),
        # No trash backend here, so the permanent downgrade needs consent; this
        # stands in for the user answering "yes" once for the batch.
        patch("src.analyze._permanent_fallback_consent", return_value=lambda _p: True),
        patch("src.analyze.play_delete") as mock_play_delete,
    ):
        outcome = _delete_analyze_paths([chrome_root, firefox_root])

    # Both roots count as deleted even though the roots themselves survive: what
    # the user asked for (the cache under them) is gone.
    assert (outcome.deleted, outcome.failed) == (2, 0)
    assert outcome.freed_bytes > 0

    assert chrome_root.exists()
    assert chrome_profile_dir.exists()
    assert chrome_login_db.exists()
    assert not chrome_cache_dir.exists()
    assert not chrome_code_cache_dir.exists()
    assert firefox_root.exists()
    assert firefox_profile_dir.exists()
    assert firefox_login_db.exists()
    assert not firefox_cache_dir.exists()
    assert not firefox_startup_cache_dir.exists()
    mock_play_delete.assert_called_once()


def test_analyze_delete_keeps_data_when_permanent_fallback_is_declined(test_env):
    """Without a trash backend and without consent, nothing is deleted (M-1)."""
    target = test_env / "Downloads" / "big-blob"
    target.mkdir(parents=True)
    (target / "payload.bin").write_text("data")

    with (
        patch("src.analyze._ensure_admin_for_delete", return_value=""),
        # _which_cached() memoizes, so patching shutil.which alone is order-dependent.
        patch("src.core.file_ops._which_cached", return_value=None),
        patch("src.analyze.play_delete") as mock_play_delete,
    ):
        outcome = _delete_analyze_paths([target])

    assert not outcome
    assert (outcome.deleted, outcome.failed, outcome.freed_bytes) == (0, 1, 0)
    # The reason travels back with the outcome so the screen can put it in the
    # notice line; printing it here would be erased by the next repaint.
    assert "No trash utility available" in outcome.first_problem
    assert (target / "payload.bin").exists()
    mock_play_delete.assert_not_called()


def test_old_items_info_reports_whether_each_entry_is_a_directory(tmp_path):
    """The row icon needs is_dir, and it comes from the stat already taken.

    Smart View lists whatever iterdir() yields, directories included, so a
    90-day-old folder must not be handed to the UI looking like a file. The flag
    is derived from st_mode rather than a second is_dir() probe because the
    selector re-renders on every keystroke.
    """
    old = time.time() - 200 * 86400
    directory = tmp_path / "stale-project"
    directory.mkdir()
    (directory / "payload.bin").write_text("x")
    plain = tmp_path / "stale.mkv"
    plain.write_text("x")
    for path in (directory, plain, tmp_path):
        os.utime(path, (old, old))

    by_name = {entry["name"]: entry for entry in get_old_items_info(tmp_path)}

    assert by_name["stale-project"]["is_dir"] is True
    assert by_name["stale.mkv"]["is_dir"] is False


def test_old_items_info_sizes_rows_from_the_parent_scan(tmp_path):
    """The parent's single scan already holds every direct child's size.

    Sizing each row on its own forked the engine once per old entry, serially,
    and every one of those scans also evicted the tree the root view had just
    filled from the shared cache.
    """
    old = time.time() - 200 * 86400
    directory = tmp_path / "stale-project"
    directory.mkdir()
    plain = tmp_path / "stale.mkv"
    plain.write_text("0123456789")
    empty_dir = tmp_path / "stale-empty"
    empty_dir.mkdir()
    for path in (directory, plain, empty_dir, tmp_path):
        os.utime(path, (old, old))

    with (
        patch(
            "src.analyze.get_direct_child_sizes_fast", return_value={"stale-project": 4096}
        ) as mock_children,
        patch("src.analyze.get_size_fast") as mock_size,
    ):
        by_name = {entry["name"]: entry for entry in get_old_items_info(tmp_path)}

    mock_children.assert_called_once_with(tmp_path)
    mock_size.assert_not_called()
    assert by_name["stale-project"]["size"] == 4096
    # A file's size is in the stat already taken; a directory the engine left out
    # of a successful scan holds nothing worth reclaiming here.
    assert by_name["stale.mkv"]["size"] == 10
    assert by_name["stale-empty"]["size"] == 0


def test_old_items_info_falls_back_when_no_fast_scan_is_available(tmp_path):
    """Only a failed scan still costs a per-item measurement."""
    old = time.time() - 200 * 86400
    directory = tmp_path / "stale-project"
    directory.mkdir()
    for path in (directory, tmp_path):
        os.utime(path, (old, old))

    with (
        patch("src.analyze.get_direct_child_sizes_fast", return_value=None),
        patch("src.analyze.get_size_fast", return_value=777) as mock_size,
    ):
        rows = get_old_items_info(tmp_path)

    mock_size.assert_called_once_with(directory)
    assert rows[0]["size"] == 777


def test_percent_of_caps_a_share_that_measures_past_its_total():
    """A scanned tree sums apparent sizes while disk_usage reports allocated
    blocks, so hard links or sparse files can push the ratio just past 1."""
    assert percent_of(512, 1024) == 50
    assert percent_of(2048, 1024) == 100.0
    assert percent_of(10, 0) == 100.0
    assert percent_of(0, 0) == 0.0


def test_filesystem_used_bytes_measures_the_disk_the_row_lives_on(tmp_path):
    """Root-view shares need a per-filesystem denominator: a single "/" total
    printed 2500% for 500 GB of Home over a 20 GB root."""
    assert filesystem_used_bytes(tmp_path) > 0

    missing = tmp_path / "gone" / "deeper"
    real_usage = shutil.disk_usage

    def usage_only_for_existing(path):
        if not Path(path).exists():
            raise OSError("no such filesystem")
        return real_usage(path)

    with patch("src.analyze.shutil.disk_usage", side_effect=usage_only_for_existing):
        # An unreadable target walks up to the nearest mountable ancestor rather
        # than answering 0 and printing every row as 100%.
        assert filesystem_used_bytes(missing) == real_usage(tmp_path).used

    with patch("src.analyze.shutil.disk_usage", side_effect=OSError("nothing works")):
        assert filesystem_used_bytes(tmp_path) == 0


def test_permanent_fallback_consent_declines_without_a_terminal(capsys):
    """A non-interactive run answers "no" instead of deleting unrecoverably."""
    consent = _permanent_fallback_consent()
    with patch("src.analyze.sys.stdin.isatty", return_value=False):
        assert consent(Path("/tmp/whatever")) is False
    assert "skipping instead of deleting permanently" in capsys.readouterr().out


def test_permanent_fallback_consent_asks_once_per_batch():
    """The confirmation is asked at most once and then reused for the batch."""
    ask = MagicMock(return_value=True)
    consent = _permanent_fallback_consent(ask)
    with (
        patch("src.analyze.sys.stdin.isatty", return_value=True),
        patch("src.analyze.sys.stdout.isatty", return_value=True),
    ):
        assert consent(Path("/tmp/one")) is True
        assert consent(Path("/tmp/two")) is True
    ask.assert_called_once_with(PERMANENT_DELETE_QUESTION)


def test_permanent_fallback_consent_remembers_a_refusal():
    """A refusal is also remembered, so the user is not asked repeatedly."""
    ask = MagicMock(return_value=False)
    consent = _permanent_fallback_consent(ask)
    with (
        patch("src.analyze.sys.stdin.isatty", return_value=True),
        patch("src.analyze.sys.stdout.isatty", return_value=True),
    ):
        assert consent(Path("/tmp/one")) is False
        assert consent(Path("/tmp/two")) is False
    assert ask.call_count == 1


def test_permanent_fallback_consent_declines_when_no_way_to_ask(capsys):
    """A caller that supplies no dialog is treated like a run with no terminal.

    core decides the question is warranted; putting it up is the UI's job. With
    nobody able to ask, the recoverable answer stands even on a real terminal.
    """
    consent = _permanent_fallback_consent()
    with (
        patch("src.analyze.sys.stdin.isatty", return_value=True),
        patch("src.analyze.sys.stdout.isatty", return_value=True),
    ):
        assert consent(Path("/tmp/whatever")) is False
    assert "skipping instead of deleting permanently" in capsys.readouterr().out


def test_screen_confirm_drives_the_selector_dialog():
    """The injected asker is what actually puts ConfirmSelector on screen."""
    with patch("src.ui.screens.analyze.ConfirmSelector") as mock_confirm:
        mock_confirm.return_value.run.return_value = True
        assert _confirm_permanent_delete("Delete permanently?") is True
    mock_confirm.assert_called_once_with("Delete permanently?")


def test_open_reports_a_missing_xdg_open_instead_of_doing_nothing(tmp_path):
    """Debian's minimal and server images ship without xdg-utils, where pressing
    "open" used to do nothing at all with no explanation."""
    target = tmp_path / "file.txt"
    target.write_text("x")

    with patch("src.ui.screens.analyze.run_command") as mock_run:
        mock_run.return_value = SimpleNamespace(ok=False)
        assert _open_in_file_manager(target) == _warn_notice(XDG_OPEN_MISSING_NOTICE)

        # A file opens its containing directory, not itself.
        assert mock_run.call_args.args[0] == ["xdg-open", str(tmp_path)]
        # Without a desktop session xdg-open falls through to a terminal handler
        # that would otherwise read the keys this screen is waiting for.
        assert mock_run.call_args.kwargs["detach_stdin"] is True

        mock_run.return_value = SimpleNamespace(ok=True)
        assert _open_in_file_manager(target) == ""


def test_analyze_delete_system_path_requires_admin():
    target = Path("/var/cache/topo-test")

    with (
        patch("src.analyze.get_size_fast", return_value=4096),
        patch("src.analyze._ensure_admin_for_delete", return_value="") as mock_admin_check,
        patch("src.analyze.safe_remove") as mock_safe,
        patch("src.analyze._sudo_remove", return_value=(True, 4096, "")) as mock_sudo,
    ):
        outcome = _delete_analyze_paths([target])

    assert (outcome.deleted, outcome.failed, outcome.freed_bytes) == (1, 0, 4096)
    mock_admin_check.assert_called_once_with([target])
    mock_sudo.assert_called_once_with(target)
    mock_safe.assert_not_called()


def test_needs_admin_for_deletion_rejects_non_home_path():
    assert _needs_admin_for_deletion(Path("/usr/share/topo-test")) is True


def test_sudo_remove_operates_on_resolved_root_managed_path():
    """The validated object is the exact path handed to privileged rm."""
    real_dir = Path("/var/tmp/topo-test-target")
    link = Path("/var/tmp/topo-test-link")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(ok=True)

    with (
        patch("src.analyze.run_command", side_effect=fake_run),
        patch("src.analyze.get_size_fast", return_value=0),
        patch("src.analyze.validate_path_for_deletion", return_value=(True, "")),
        patch.object(
            Path,
            "lstat",
            return_value=type("Stat", (), {"st_mode": stat.S_IFDIR | 0o755, "st_uid": 0})(),
        ),
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "is_symlink", return_value=False),
        patch.object(Path, "resolve", side_effect=lambda strict=False: real_dir),
    ):
        assert _sudo_remove(link) == (True, 0, "")

    # rm must target the resolved real directory, never the raw symlink path.
    assert captured["cmd"] == ["rm", "-rf", "--one-file-system", "--", str(real_dir)]


def test_sudo_remove_rejects_user_writable_ancestor(test_env):
    target = test_env / "mutable-parent" / "target"
    target.mkdir(parents=True)

    with (
        patch("src.analyze.run_command") as mock_run,
        patch("src.analyze.get_size_fast", return_value=0),
    ):
        removed, freed, problem = _sudo_remove(target)

    assert (removed, freed) == (False, 0)
    # The refusal is described in the returned reason, which the notice line
    # shows; nothing is printed from down here.
    assert "untrusted ancestor directory" in problem
    mock_run.assert_not_called()


def test_delete_notice_reports_success_partial_and_total_failure():
    """One frame-safe line per delete outcome (I3).

    Analyze repaints the whole frame right after a delete, so the batch's own
    prints were overwritten within the same tick: a success said nothing at all
    and a refusal flashed. Each outcome now becomes the notice the *next* frame
    draws, and the glyph is what tells them apart.
    """
    deleted_all = _delete_notice(DeleteOutcome(deleted=3, freed_bytes=2 * 1024**3))
    assert "✓" in deleted_all
    assert "Deleted 3 item(s), freed 2.0 GiB." in ANSI_CSI_RE.sub("", deleted_all)

    partial = _delete_notice(
        DeleteOutcome(deleted=1, failed=2, freed_bytes=1024, first_problem="Skipped /x: busy")
    )
    assert "⚠" in partial
    assert "2 left: Skipped /x: busy" in ANSI_CSI_RE.sub("", partial)

    failed = _delete_notice(DeleteOutcome(failed=2, first_problem="Skipped /x: busy"))
    assert "✗" in failed
    assert "2 item(s) not deleted: Skipped /x: busy" in ANSI_CSI_RE.sub("", failed)

    # Nothing was even attempted: the admin prompt was declined. The reason is
    # the whole message -- there are no counts worth reporting.
    declined = _delete_notice(DeleteOutcome(first_problem=DELETE_CANCELLED_PROBLEM))
    assert "✗" in declined
    assert DELETE_CANCELLED_PROBLEM in ANSI_CSI_RE.sub("", declined)


def test_notice_stays_on_one_row_so_the_frame_below_it_does_not_shift():
    """A wrapped notice pushes every row of the absolutely positioned frame down.

    Problems can carry a path, which has no length limit, so the text is clamped
    here rather than trusted.
    """
    long_problem = "Skipped " + "/very-long-directory-name" * 20 + ": busy"
    notice = ANSI_CSI_RE.sub("", _fail_notice(long_problem))

    assert notice.endswith("…")
    # The glyph and its space ride on top of the clamped text.
    assert len(notice) == NOTICE_TEXT_LIMIT + 2
    assert _fail_notice("") == ""


@patch("src.ui.screens.analyze.get_fast_explore_data", return_value=None)
@patch("src.ui.screens.analyze.get_rust_tree_data", return_value=None)
def test_failed_root_scan_explains_itself_and_waits_for_a_keypress(
    mock_tree, mock_fallback, test_env, capsys
):
    """A dead scan with nowhere to go back to must say why before it closes (I3).

    It used to print "❌ Engine scan failed." and sleep 1.5 s -- the sleep was the
    only reason the line was readable at all, since the next frame would have
    overwritten it, and on the root view (no state to pop) the screen simply
    closed. Now the reason waits for Enter like every other terminal notice.
    """
    ScanCache.clear()

    with patch("src.ui.screens.analyze.Navigator.wait_for_return") as wait:
        run_deep_analysis()

    wait.assert_called_once_with()
    assert ENGINE_SCAN_FAILED_NOTICE in ANSI_CSI_RE.sub("", capsys.readouterr().out)
