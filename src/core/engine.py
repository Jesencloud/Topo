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
from typing import cast

from .scan_cache import ScanCache, ScanResult
from .system import run_command

# How long a single topo-core invocation may take before it is abandoned.
_SCAN_COMMAND_TIMEOUT = 300

# The stdout JSON contract this build speaks. topo-core wraps every mode's output
# as {"schema_version": N, "data": <payload>}; we accept a payload only when N
# equals this, and otherwise fall back to the pure-Python walk. Must equal
# scanner.rs's SCHEMA_VERSION -- the release process pins one engine to one
# checkout, so a mismatch means a foreign or stale binary, which this refuses to
# misread. Bump both together whenever the payload shape changes.
ENGINE_SCHEMA_VERSION = 1


# The only architectures an engine is built for. `platform.machine()` values, so
# arm64 is in here for the platforms that spell aarch64 that way, and the two
# names install.sh knows are the two names this table maps.
_ENGINE_BY_ARCH = {
    "x86_64": "topo-core-x86_64",
    "aarch64": "topo-core-aarch64",
    "arm64": "topo-core-aarch64",
}


@functools.cache
def get_core_binary() -> Path | None:
    """The topo-core binary for this machine, or None when there is no such thing.

    None is the honest answer on riscv64, armv7l or i686: the source archive
    carries both engines, so anything that merely *picks a name* finds a file
    there and returns a binary the kernel refuses to exec. Callers already treat
    None as "use the pure-Python path", which is what those machines get either
    way -- the difference is that they no longer pay for a failed exec per scan,
    and `topo doctor` reports a missing engine instead of a broken one.

    Deliberately no glob fallback: it only ever ran when the engine for this
    architecture was absent, and then the only thing left to find was an engine
    for the other one.
    """
    name = _ENGINE_BY_ARCH.get(platform.machine().lower())
    if name is None:
        return None
    binary = Path(__file__).parent / "bin" / name
    return binary if binary.is_file() else None


def normalize_scan_path(path: str | Path) -> Path:
    """Return one stable absolute cache/process key without leaking resolve errors."""
    raw = Path(path).expanduser()
    try:
        return raw.resolve(strict=False)
    except (OSError, RuntimeError):
        return raw.absolute()


def _unwrap_engine_payload(stdout: str) -> object | None:
    """Return the engine's payload from its versioned envelope, or None.

    Every topo-core mode prints {"schema_version": N, "data": <payload>}. This
    returns <payload> only when N is the version this build understands
    (ENGINE_SCHEMA_VERSION). A non-JSON, non-object, or version-mismatched
    envelope -- a truncated write, or a foreign/stale engine binary -- yields
    None, and every caller reads None as "use the pure-Python path" rather than
    trust a shape it cannot verify. The payload itself is still unchecked here;
    callers guard its type (get_rust_scan_data's isinstance, get_rust_tree_data's
    dict/`.`(dict) pair), so this only adds the version gate in front of them.
    """
    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(envelope, dict):
        return None
    if envelope.get("schema_version") != ENGINE_SCHEMA_VERSION:
        return None
    return envelope.get("data")


def get_rust_scan_data(path: Path, *, use_cache: bool = True) -> ScanResult | None:
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
        data = _unwrap_engine_payload(res.stdout)
        # A well-formed but non-object payload (a bare number, string, or array)
        # survives the envelope's version gate, so ScanCache.set and every caller
        # -- which expect a dict and would raise AttributeError on .get() -- are
        # still shielded here. get_rust_tree_data guards the same way; keep the
        # three entry points symmetric rather than trusting the payload's shape.
        if not isinstance(data, dict):
            return None
        # isinstance narrows _unwrap_engine_payload's object to dict[Any, Any];
        # the ScanResult shape itself is still a promise the release process keeps,
        # not a check (see ScanResult's docstring), so this cast asserts nothing new.
        result = cast(ScanResult, data)
        ScanCache.set(path, result)
        return result
    return None


def get_rust_tree_data(path: Path) -> ScanResult | None:
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
    tree = _unwrap_engine_payload(res.stdout)
    if not isinstance(tree, dict) or not isinstance(tree.get("."), dict):
        return None
    root_data: ScanResult | None = None
    for relative, aggregate in tree.items():
        if not isinstance(aggregate, dict):
            continue
        node = path if relative == "." else path / relative
        # A DirAgg carries no path of its own and omits top_files unless the node
        # has any (only the root aggregate does), so those two are filled in here.
        # The engine's own size estimate rides along because ScanCache prefers it
        # to walking the record itself.
        data_item: ScanResult = {
            "path": str(node),
            "total_size_bytes": aggregate.get("total_size_bytes", 0),
            "file_count": aggregate.get("file_count", 0),
            "top_files": aggregate.get("top_files", []),
            "subdirs": aggregate.get("subdirs", {}),
            "_cache_estimated_bytes": aggregate.get("_cache_estimated_bytes", 0),
        }
        if relative == ".":
            root_data = data_item
            continue
        ScanCache.set(node, data_item)
    if root_data:
        ScanCache.set(path, root_data)
    return root_data or ScanCache.get(path)
