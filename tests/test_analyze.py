import json
import stat
import subprocess
import sys
import threading
import time
from functools import cache
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.analyze import (
    _delete_analyze_paths,
    _direct_child_count_exceeds,
    _explore_notice,
    _needs_admin_for_deletion,
    _normalize_scan_path,
    _parallel_scan_sizes,
    _permanent_fallback_consent,
    _render_scan_header,
    _scan_status_message,
    _scan_with_spinner,
    _should_use_fast_explore,
    _sudo_remove,
    build_analysis_entry,
    build_linux_insights,
    get_fast_explore_data,
    get_rust_scan_data,
    get_rust_tree_data,
    run_deep_analysis,
)
from src.core.file_ops import CACHEDIR_TAG_SIGNATURE, has_valid_cachedir_tag
from src.core.scan_cache import ScanCache


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

    Deliberately not ``_get_core_binary()``: that returns the *packaged* binary
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

    root = _normalize_scan_path(tmp_path)
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

    ``_parallel_scan_sizes`` seeds the cache from a ThreadPoolExecutor (several
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
        patch("src.core.analyze._get_core_binary", return_value=Path("/tmp/topo-core")),
        patch(
            "src.core.analyze.run_command",
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

    with patch("src.core.analyze.get_rust_tree_data", side_effect=fake_tree):
        sizes = _parallel_scan_sizes([root, child, other, child])

    assert scanned == [root, other]
    assert sizes == {root: 100, child: 25, other: 100}


def test_parallel_scan_sizes_notifies_only_when_scanning():
    root = Path("/home/user")
    notice = MagicMock()
    ScanCache.clear()

    def fake_tree(path):
        ScanCache.set(path, {"total_size_bytes": 100})

    with patch("src.core.analyze.get_rust_tree_data", side_effect=fake_tree):
        assert _parallel_scan_sizes([root], on_scan_start=notice) == {root: 100}
        assert _parallel_scan_sizes([root], on_scan_start=notice) == {root: 100}

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
        patch("src.core.analyze.get_rust_tree_data", side_effect=tree_seeds_only_a),
        patch("src.core.analyze.get_rust_scan_data", side_effect=scan_seeds_b),
    ):
        sizes = _parallel_scan_sizes([a, b], on_scan_start=notice)

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

    with patch("src.core.analyze.get_rust_tree_data", side_effect=fake_tree):
        sizes = _parallel_scan_sizes([other, hub, models])

    # hub and models are cache hits -> only /usr is scanned.
    assert scanned == [other]
    assert sizes == {other: 100, hub: 500, models: 300}


def test_parallel_scan_sizes_matches_symlinked_input_to_resolved_cache_key(tmp_path):
    """The scan functions key the cache by the normalized (symlink-resolved)
    path, so _parallel_scan_sizes must probe under the same key. A symlinked
    input (e.g. ~/.cache moved to another partition) would otherwise be seeded
    under its resolved key but read under the raw key, silently reporting 0."""
    ScanCache.clear()
    real = tmp_path / "real_cache"
    real.mkdir()
    link = tmp_path / "linked_cache"
    link.symlink_to(real, target_is_directory=True)
    assert _normalize_scan_path(link) != link  # guard: the symlink must resolve

    def fake_tree(path):
        # Mimic get_rust_tree_data: cache under the normalized (resolved) key.
        ScanCache.set(_normalize_scan_path(path), {"total_size_bytes": 4096})

    with patch("src.core.analyze.get_rust_tree_data", side_effect=fake_tree):
        sizes = _parallel_scan_sizes([link])

    # Keyed by the caller's original path, with the size found via the resolved key.
    assert sizes == {link: 4096}


def test_get_rust_tree_data_survives_lru_eviction(test_env):
    """The tree result must return its root even if cache capacity is exceeded."""
    from src.core.analyze import get_rust_tree_data

    large_tree = {
        ".": {"total_size_bytes": 1000, "file_count": 10, "subdirs": {}},
    }
    for i in range(12):
        large_tree[f"sub_{i}"] = {"total_size_bytes": 1, "file_count": 1, "subdirs": {}}

    fake_res = MagicMock()
    fake_res.ok = True
    fake_res.stdout = json.dumps(large_tree)

    with (
        patch("src.core.analyze.run_command", return_value=fake_res),
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
        patch("src.core.analyze.run_command", return_value=fake_res),
        patch.object(ScanCache, "MAX_ESTIMATED_BYTES", 1),
    ):
        result = get_rust_tree_data(test_env)

    assert result is not None
    assert result["total_size_bytes"] == 1000
    assert ScanCache.get(test_env.resolve()) is None


def test_normalize_scan_path_falls_back_when_resolve_fails(tmp_path):
    relative = Path("relative-target")
    with patch.object(Path, "resolve", side_effect=OSError("unavailable")):
        normalized = _normalize_scan_path(relative)

    assert normalized == relative.absolute()


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
    # Clears the screen and homes the cursor (CLEAR_SCREEN = \033[2J\033[H).
    assert output.startswith("\033[2J\033[H")
    assert "Analyze Disk" in output
    # The title must sit on row 2 (one blank line above it), matching
    # AnalyzeSelector.render(), so the screen does not shift vertically when
    # the scan screen hands off to the result list.
    after_home = output.split("\033[H")[-1]
    assert after_home[: after_home.index("Analyze Disk")].count("\n") == 1


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._should_use_fast_explore", return_value=True)
@patch("src.core.analyze._get_rust_scan_data_with_spinner")
def test_fast_explore_ignores_rust_scan_cache(
    mock_single, _mock_should_fast, mock_selector, test_env
):
    """Wide-directory preview should show the live direct listing, not stale Rust cache data."""
    ScanCache.clear()
    dir_a = test_env / "A"
    dir_b = dir_a / "B"
    dir_b.mkdir(parents=True)
    (dir_a / "fresh.txt").write_bytes(b"fresh")
    ScanCache.set(dir_a, {"total_size_bytes": 1000, "subdirs": {"stale-only": 1000}})
    ScanCache.set(dir_b, {"total_size_bytes": 500, "subdirs": {}})

    mock_selector.return_value.run.side_effect = [("DRILL_DOWN", 0), ("QUIT", None)]

    run_deep_analysis(dir_a)

    mock_single.assert_not_called()
    first_items = mock_selector.call_args_list[0].args[1]
    shown_names = {item["name"] for item in first_items}
    assert shown_names == {"B", "fresh.txt"}


def test_should_use_fast_explore_only_for_wide_directories(test_env):
    root = test_env / "wide"
    root.mkdir()
    for index in range(3):
        (root / f"item-{index}").write_text("x")

    assert _direct_child_count_exceeds(root, limit=2) is True
    assert _direct_child_count_exceeds(root, limit=3) is False
    assert _should_use_fast_explore(root, direct_entry_limit=2) is True
    assert _should_use_fast_explore(root, direct_entry_limit=3) is False


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


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._should_use_fast_explore", return_value=False)
@patch("src.core.analyze._get_rust_scan_data_with_spinner")
def test_regular_directory_uses_rust_size_view(
    mock_single, _mock_should_fast, mock_selector, test_env
):
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


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._should_use_fast_explore", return_value=True)
@patch("src.core.analyze._get_rust_scan_data_with_spinner")
@patch("src.core.analyze.build_analysis_entry")
def test_fast_explore_builds_rows_without_per_path_analysis(
    mock_build_entry, mock_single, _mock_should_fast, mock_selector, test_env
):
    ScanCache.clear()
    directory = test_env / "many"
    (directory / "nested").mkdir(parents=True)
    (directory / "item.txt").write_bytes(b"x")
    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis(directory)

    mock_single.assert_not_called()
    mock_build_entry.assert_not_called()


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._should_use_fast_explore", return_value=True)
@patch("src.core.analyze._get_rust_scan_data_with_spinner")
def test_wide_cache_directory_uses_fast_explore_not_rust(
    mock_single, _mock_should_fast, mock_selector, test_env
):
    ScanCache.clear()
    icon_cache = test_env / ".cache/gnome-software/icons"
    icon_cache.mkdir(parents=True)
    for index in range(3):
        (icon_cache / f"icon-{index}.png").write_bytes(b"x")
    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis(icon_cache)

    mock_single.assert_not_called()
    shown_names = {item["name"] for item in mock_selector.call_args.args[1]}
    assert "icon-0.png" in shown_names


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._parallel_scan_sizes")
@patch("src.core.analyze.get_rust_tree_data")
def test_root_view_uses_tree_scan(mock_tree, mock_parallel, mock_selector, test_env):
    ScanCache.clear()
    mock_tree.return_value = {"total_size_bytes": 1000, "subdirs": {}, "top_files": []}
    mock_parallel.return_value = {}
    mock_selector.return_value.run.side_effect = [("QUIT", None)]

    run_deep_analysis()  # no target_path -> root view (current_target is None)

    mock_tree.assert_called_once_with(test_env)


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._parallel_scan_sizes")
@patch("src.core.analyze.get_rust_tree_data")
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


@patch("src.core.analyze.AnalyzeSelector")
@patch("src.core.analyze._parallel_scan_sizes")
@patch("src.core.analyze.get_fast_explore_data")
@patch("src.core.analyze.get_rust_tree_data")
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
    assert entry["icon"] == "🗂️"
    assert entry["percent"] == 50


def test_build_analysis_entry_marks_browser_cache_as_cleanable(test_env):
    cache_dir = test_env / ".mozilla/firefox/profile.default/cache2"
    cache_dir.mkdir(parents=True)

    entry = build_analysis_entry("Cache", cache_dir, size=512, total_size=1024)

    assert entry["is_cleanable"] is True
    assert entry["cleanable_reason"] == "App cache"
    assert entry["icon"] == "🗂️"


def test_build_analysis_entry_marks_desktop_app_cache_as_cleanable(test_env):
    cache_dir = test_env / ".cache/spotify/Data"
    cache_dir.mkdir(parents=True)

    entry = build_analysis_entry("Data", cache_dir, size=512, total_size=1024)

    assert entry["is_cleanable"] is True
    assert entry["cleanable_reason"] == "App cache"
    assert entry["icon"] == "🗂️"


def test_build_analysis_entry_marks_generic_xdg_cache_as_cleanable(test_env):
    cache_dir = test_env / ".cache/random-tool"
    cache_dir.mkdir(parents=True)

    entry = build_analysis_entry("random-tool", cache_dir, size=512, total_size=1024)
    root_entry = build_analysis_entry(".cache", test_env / ".cache", size=512, total_size=1024)

    assert entry["is_cleanable"] is True
    assert entry["cleanable_reason"] == "XDG cache"
    assert entry["icon"] == "🗂️"
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
        patch("src.core.analyze._ensure_admin_for_delete", return_value=True) as mock_admin_check,
        patch("src.core.analyze.safe_remove", return_value=(True, "Moved to trash")) as mock_safe,
        patch("src.core.analyze._sudo_remove") as mock_sudo,
    ):
        assert _delete_analyze_paths([target]) is True

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
        patch("src.core.analyze._ensure_admin_for_delete", return_value=True),
        # _which_cached() memoizes, so patching shutil.which alone is order-dependent.
        patch("src.core.file_ops._which_cached", return_value=None),
        # No trash backend here, so the permanent downgrade needs consent; this
        # stands in for the user answering "yes" once for the batch.
        patch("src.core.analyze._permanent_fallback_consent", return_value=lambda _p: True),
        patch("src.core.analyze.Navigator.play_delete") as mock_play_delete,
    ):
        assert _delete_analyze_paths([chrome_root, firefox_root]) is True

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


def test_analyze_delete_keeps_data_when_permanent_fallback_is_declined(test_env, capsys):
    """Without a trash backend and without consent, nothing is deleted (M-1)."""
    target = test_env / "Downloads" / "big-blob"
    target.mkdir(parents=True)
    (target / "payload.bin").write_text("data")

    with (
        patch("src.core.analyze._ensure_admin_for_delete", return_value=True),
        # _which_cached() memoizes, so patching shutil.which alone is order-dependent.
        patch("src.core.file_ops._which_cached", return_value=None),
        patch("src.core.analyze.Navigator.play_delete") as mock_play_delete,
    ):
        assert _delete_analyze_paths([target]) is False

    assert (target / "payload.bin").exists()
    mock_play_delete.assert_not_called()
    assert "No trash utility available" in capsys.readouterr().out


def test_permanent_fallback_consent_declines_without_a_terminal(capsys):
    """A non-interactive run answers "no" instead of deleting unrecoverably."""
    consent = _permanent_fallback_consent()
    with patch("src.core.analyze.sys.stdin.isatty", return_value=False):
        assert consent(Path("/tmp/whatever")) is False
    assert "skipping instead of deleting permanently" in capsys.readouterr().out


def test_permanent_fallback_consent_asks_once_per_batch():
    """The confirmation is asked at most once and then reused for the batch."""
    consent = _permanent_fallback_consent()
    with (
        patch("src.core.analyze.sys.stdin.isatty", return_value=True),
        patch("src.core.analyze.sys.stdout.isatty", return_value=True),
        patch("src.core.analyze.ConfirmSelector") as mock_confirm,
    ):
        mock_confirm.return_value.run.return_value = True
        assert consent(Path("/tmp/one")) is True
        assert consent(Path("/tmp/two")) is True
    assert mock_confirm.call_count == 1


def test_permanent_fallback_consent_remembers_a_refusal():
    """A refusal is also remembered, so the user is not asked repeatedly."""
    consent = _permanent_fallback_consent()
    with (
        patch("src.core.analyze.sys.stdin.isatty", return_value=True),
        patch("src.core.analyze.sys.stdout.isatty", return_value=True),
        patch("src.core.analyze.ConfirmSelector") as mock_confirm,
    ):
        mock_confirm.return_value.run.return_value = False
        assert consent(Path("/tmp/one")) is False
        assert consent(Path("/tmp/two")) is False
    assert mock_confirm.call_count == 1


def test_analyze_delete_system_path_requires_admin():
    target = Path("/var/cache/topo-test")

    with (
        patch("src.core.analyze.get_size_fast", return_value=4096),
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


def test_sudo_remove_operates_on_resolved_root_managed_path():
    """The validated object is the exact path handed to privileged rm."""
    real_dir = Path("/var/tmp/topo-test-target")
    link = Path("/var/tmp/topo-test-link")

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return MagicMock(ok=True)

    with (
        patch("src.core.analyze.run_command", side_effect=fake_run),
        patch("src.core.analyze.get_size_fast", return_value=0),
        patch("src.core.analyze.validate_path_for_deletion", return_value=(True, "")),
        patch.object(
            Path,
            "lstat",
            return_value=type("Stat", (), {"st_mode": stat.S_IFDIR | 0o755, "st_uid": 0})(),
        ),
        patch.object(Path, "exists", return_value=True),
        patch.object(Path, "is_symlink", return_value=False),
        patch.object(Path, "resolve", side_effect=lambda strict=False: real_dir),
    ):
        assert _sudo_remove(link) is True

    # rm must target the resolved real directory, never the raw symlink path.
    assert captured["cmd"] == ["rm", "-rf", "--one-file-system", "--", str(real_dir)]


def test_sudo_remove_rejects_user_writable_ancestor(test_env):
    target = test_env / "mutable-parent" / "target"
    target.mkdir(parents=True)

    with (
        patch("src.core.analyze.run_command") as mock_run,
        patch("src.core.analyze.get_size_fast", return_value=0),
    ):
        assert _sudo_remove(target) is False

    mock_run.assert_not_called()
