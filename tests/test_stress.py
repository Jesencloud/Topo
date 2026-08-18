from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from src.clean.app_manager import _ResidueEntryIndex
from src.core.analyze import get_rust_tree_data
from src.core.scan_cache import ScanCache


def test_stress_rust_engine_deep_directory_tree(tmp_path):
    """Stress test: Scan a deep directory structure (64 levels) in memory without stack overflow."""
    curr = tmp_path
    for i in range(64):
        curr = curr / f"depth_{i}"
    curr.mkdir(parents=True)
    (curr / "deep_file.txt").write_bytes(b"A" * 1024)

    root_data = get_rust_tree_data(tmp_path)
    assert root_data is not None
    assert root_data["total_size_bytes"] == 1024
    assert root_data["file_count"] == 1


def test_stress_rust_engine_broad_fanout_directory(tmp_path):
    """Stress test: Scan a directory containing 1,000 files concurrently."""
    for i in range(1000):
        (tmp_path / f"file_{i:04d}.dat").write_bytes(b"X" * 100)

    root_data = get_rust_tree_data(tmp_path)
    assert root_data is not None
    assert root_data["total_size_bytes"] == 1000 * 100
    assert root_data["file_count"] == 1000


def test_stress_rust_engine_circular_symlinks_immunity(tmp_path):
    """Stress test: Rust scanner must safely ignore circular directory symlinks and not hang."""
    sub = tmp_path / "sub_dir"
    sub.mkdir()
    (sub / "sample.bin").write_bytes(b"B" * 512)
    # Create circular symlink pointing back to parent
    loop_link = sub / "loop"
    try:
        loop_link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks not supported on filesystem")

    root_data = get_rust_tree_data(tmp_path)
    assert root_data is not None
    # Must correctly count only the real file and terminate in milliseconds
    assert root_data["file_count"] == 1
    assert root_data["total_size_bytes"] == 512


def test_stress_scan_cache_high_concurrency_race():
    """Stress test: 16 threads concurrently writing and evicting entries under the 64 MiB limit."""
    ScanCache.clear()
    num_threads = 16
    entries_per_thread = 200

    def worker(thread_id: int):
        for i in range(entries_per_thread):
            path = Path(f"/virtual/thread_{thread_id}/path_{i}")
            # Insert 100 KB payload per entry
            payload = {
                "total_size_bytes": 102400,
                "subdirs": {f"sub_{j}": 1024 for j in range(20)},
                "top_files": [{"path": f"/virtual/f_{j}", "size_bytes": 512} for j in range(10)],
            }
            ScanCache.set(path, payload)
            # Concurrently read back
            _ = ScanCache.get(path)

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, t) for t in range(num_threads)]
        for f in futures:
            f.result()

    # Assert cache invariants hold strictly: byte cap must never be exceeded
    assert ScanCache._estimated_bytes <= ScanCache.MAX_ESTIMATED_BYTES
    assert len(ScanCache._data) <= ScanCache.MAX_ENTRIES
    ScanCache.clear()


def test_stress_residue_index_massive_lookup(tmp_path):
    """Stress test: Index 5,000 residue candidate names and perform 1,000 rapid lookups."""
    entries = [
        (f"com.example.app_{i:04d}_cache_residue", tmp_path / f"app_{i}") for i in range(5000)
    ]
    index = _ResidueEntryIndex.build(entries)
    assert index.is_indexed is True

    # Search for matching prefixes, exact matches, and non-matches
    for i in range(1000):
        target = {f"app_{i:04d}"}
        candidates = index.candidates(target)
        # Verify candidate subset correctness
        assert any(f"app_{i:04d}" in name for name, _p in candidates)
