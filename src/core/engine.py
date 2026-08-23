"""The topo-core interface: locate the Rust scanner and run it.

Infrastructure rather than part of Analyze, even though Analyze is its loudest
caller: file_ops reaches for the same scanner to size a directory quickly, and
doctor only wants to know the binary is there at all. While this lived in
core.analyze both had to import a feature module to reach it, and file_ops had
to do so from inside a function to dodge the resulting cycle -- a lazy import is
usually a cycle wearing a disguise.

The binary still resolves against this file's own directory, which is the same
src/core/ as before, so src/core/bin/ is untouched.
"""

import functools
import json
import platform
from pathlib import Path
from typing import Any

from .scan_cache import ScanCache
from .system import run_command

# How long a single topo-core invocation may take before it is abandoned.
_SCAN_COMMAND_TIMEOUT = 300


@functools.cache
def get_core_binary() -> Path | None:
    """Resolves the architecture-specific topo-core binary path.

    install.sh keeps only the binary matching the host arch (e.g. it removes
    topo-core-x86_64 on ARM64), so we must pick the name dynamically. Falls back
    to any available engine binary for dev/single-arch checkouts.
    """
    bin_dir = Path(__file__).parent / "bin"
    arch = platform.machine().lower()
    suffix = "aarch64" if arch in ("aarch64", "arm64") else "x86_64"
    preferred = bin_dir / f"topo-core-{suffix}"
    if preferred.exists():
        return preferred
    for candidate in sorted(bin_dir.glob("topo-core-*")):
        if candidate.is_file():
            return candidate
    return None


def normalize_scan_path(path: str | Path) -> Path:
    """Return one stable absolute cache/process key without leaking resolve errors."""
    raw = Path(path).expanduser()
    try:
        return raw.resolve(strict=False)
    except (OSError, RuntimeError):
        return raw.absolute()


def get_rust_scan_data(path: Path, *, use_cache: bool = True) -> dict[str, Any] | None:
    """Calls the architecture-specific topo-core binary and returns parsed JSON."""
    binary = get_core_binary()
    if binary is None:
        return None

    path = normalize_scan_path(path)
    # Check cache first
    cached = ScanCache.get(path)
    if use_cache and cached:
        return cached

    res = run_command([str(binary), str(path)], capture=True, timeout=_SCAN_COMMAND_TIMEOUT)
    if res.ok:
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError:
            return None
        ScanCache.set(path, data)
        return data
    return None


def get_rust_tree_data(path: Path) -> dict[str, Any] | None:
    """Scan once and seed ScanCache for every significant descendant."""
    binary = get_core_binary()
    if binary is None:
        return None

    path = normalize_scan_path(path)
    res = run_command(
        [str(binary), "--tree", str(path)],
        capture=True,
        timeout=_SCAN_COMMAND_TIMEOUT,
    )
    if not res.ok:
        return None
    try:
        tree = json.loads(res.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(tree, dict) or not isinstance(tree.get("."), dict):
        return None
    root_data = None
    for relative, aggregate in tree.items():
        if not isinstance(aggregate, dict):
            continue
        node = path if relative == "." else path / relative
        data_item = {"path": str(node), "top_files": [], **aggregate}
        if relative == ".":
            root_data = data_item
            continue
        ScanCache.set(node, data_item)
    if root_data:
        ScanCache.set(path, root_data)
    return root_data or ScanCache.get(path)
