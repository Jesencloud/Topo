"""Which machines get a Rust engine, and what the rest get instead.

`get_core_binary()` used to answer with a name it derived by elimination --
aarch64 for the two spellings of it, x86_64 for *everything else*. So riscv64,
armv7l and i686 were handed the x86_64 engine that the source archive carries,
and paid an `Exec format error` per scan instead of falling back to the
pure-Python path the callers already have for a missing engine.

install.sh reaches the same conclusion from the same list, and the tests here
require the two to keep naming the same things.
"""

import json
import platform
import re
import subprocess
from pathlib import Path

import pytest

from src.core import engine
from src.core.system import CommandResult

REPO_ROOT = Path(__file__).resolve().parents[1]


def _envelope(data, version=engine.ENGINE_SCHEMA_VERSION):
    """The engine's stdout for one payload: a versioned {schema_version, data}."""
    return json.dumps({"schema_version": version, "data": data})


def _scan_payload(path="/x", total_size_bytes=5):
    """A minimal well-formed ScanResult, for tests about the boundary not the shape."""
    return {
        "path": str(path),
        "total_size_bytes": total_size_bytes,
        "file_count": 1,
        "subdirs": {},
        "top_files": [],
    }


def _mock_engine_stdout(monkeypatch, tmp_path, stdout):
    """Stand up a fake x86_64 engine whose one call returns `stdout`, exit 0.

    Returns the normalized scan path and leaves ScanCache empty so a test can
    assert the boundary neither returned nor cached the payload.
    """
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(engine, "__file__", str(tmp_path / "engine.py"))
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "bin" / "topo-core-x86_64").write_bytes(b"\x7fELF")
    monkeypatch.setattr(
        engine,
        "run_command",
        lambda *a, **k: CommandResult(args=list(a[0]), returncode=0, stdout=stdout),
    )
    engine.ScanCache.clear()
    return engine.normalize_scan_path(tmp_path)


@pytest.fixture(autouse=True)
def _clear_engine_cache():
    # get_core_binary() is functools.cache'd, so a patched platform.machine()
    # would otherwise be answered from the previous test's lookup.
    engine.get_core_binary.cache_clear()
    yield
    engine.get_core_binary.cache_clear()


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        ("x86_64", "topo-core-x86_64"),
        ("aarch64", "topo-core-aarch64"),
        ("arm64", "topo-core-aarch64"),
        ("AArch64", "topo-core-aarch64"),
    ],
)
def test_a_supported_architecture_gets_its_own_engine(monkeypatch, tmp_path, machine, expected):
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(engine, "__file__", str(tmp_path / "engine.py"))
    (tmp_path / "bin").mkdir()
    for name in ("topo-core-x86_64", "topo-core-aarch64"):
        (tmp_path / "bin" / name).write_bytes(b"\x7fELF")

    assert engine.get_core_binary() == tmp_path / "bin" / expected


@pytest.mark.parametrize("machine", ["riscv64", "armv7l", "i686", "ppc64le", ""])
def test_an_unsupported_architecture_gets_no_engine_rather_than_the_wrong_one(
    monkeypatch, tmp_path, machine
):
    monkeypatch.setattr(platform, "machine", lambda: machine)
    monkeypatch.setattr(engine, "__file__", str(tmp_path / "engine.py"))
    (tmp_path / "bin").mkdir()
    # Both engines present, as they are in the source archive a git install
    # unpacks. Picking either one produces a binary the kernel refuses to exec.
    for name in ("topo-core-x86_64", "topo-core-aarch64"):
        (tmp_path / "bin" / name).write_bytes(b"\x7fELF")

    assert engine.get_core_binary() is None


def test_a_supported_architecture_with_no_engine_installed_also_gets_none(monkeypatch, tmp_path):
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(engine, "__file__", str(tmp_path / "engine.py"))

    assert engine.get_core_binary() is None


def test_the_scan_helpers_skip_the_subprocess_when_there_is_no_engine(monkeypatch):
    # The reason None is safe to return: every caller treats it as "use the
    # pure-Python path", and neither helper reaches run_command.
    monkeypatch.setattr(platform, "machine", lambda: "riscv64")
    monkeypatch.setattr(
        engine, "run_command", lambda *a, **k: pytest.fail("ran the engine that does not exist")
    )

    assert engine.get_rust_scan_data(Path("/tmp")) is None
    assert engine.get_rust_tree_data(Path("/tmp")) is None


@pytest.mark.parametrize("data", [123, "oops", [1, 2], None])
def test_get_rust_scan_data_rejects_a_well_formed_non_object_payload(monkeypatch, tmp_path, data):
    # A zero-exit engine whose envelope carries valid JSON that is not an object:
    # _unwrap_engine_payload passes it (the version matches), but ScanCache.set and
    # every caller call .get() on it. Without the isinstance guard this raised
    # AttributeError; get_rust_tree_data already guards the same way, so the scan
    # entry point must too -- return None (pure-Python fallback) and never seed the
    # cache with a non-dict.
    scan_path = _mock_engine_stdout(monkeypatch, tmp_path, _envelope(data))

    assert engine.get_rust_scan_data(scan_path, use_cache=False) is None
    assert engine.ScanCache.get(scan_path) is None


@pytest.mark.parametrize("stdout", ["123", '"oops"', "[1, 2]", "null", '{"total_size_bytes": 5}'])
def test_the_scan_helpers_reject_a_payload_that_is_not_a_versioned_envelope(
    monkeypatch, tmp_path, stdout
):
    # Valid JSON, exit 0, but no {"schema_version", "data"} wrapper -- what a
    # pre-envelope or foreign engine prints. The bare object case ({"total_size..."})
    # is the one that matters: its schema_version is absent (!= the expected int),
    # so the boundary refuses it instead of caching a shape it never version-checked.
    scan_path = _mock_engine_stdout(monkeypatch, tmp_path, stdout)

    assert engine.get_rust_scan_data(scan_path, use_cache=False) is None
    assert engine.get_rust_tree_data(scan_path) is None
    assert engine.ScanCache.get(scan_path) is None


def test_the_scan_helpers_reject_a_schema_version_they_do_not_understand(monkeypatch, tmp_path):
    # A future/foreign engine: correct envelope, well-formed ScanResult payload,
    # but a schema_version this build does not speak. The release process pins one
    # engine to one checkout, so this only happens with a stale or foreign binary;
    # both helpers fall back to pure Python rather than trust a contract that moved.
    payload = _scan_payload()
    other_version = engine.ENGINE_SCHEMA_VERSION + 1
    scan_path = _mock_engine_stdout(
        monkeypatch, tmp_path, _envelope(payload, version=other_version)
    )

    assert engine.get_rust_scan_data(scan_path, use_cache=False) is None
    assert engine.get_rust_tree_data(scan_path) is None
    assert engine.ScanCache.get(scan_path) is None


def test_this_host_resolves_a_bundled_engine_when_one_is_built_for_it():
    binary = engine.get_core_binary()
    if platform.machine().lower() not in engine._ENGINE_BY_ARCH:
        assert binary is None
        return
    assert binary is not None
    assert binary.parent == REPO_ROOT / "src/core/bin"


def test_install_sh_knows_the_same_architectures_by_the_same_names():
    """One list of supported architectures, in shell and in Python.

    install.sh decides three things from its copy -- which engine to download,
    which to delete, and whether an existing install is complete -- and none of
    them are visible to ruff, mypy or tach. So run the shell function against
    every architecture the Python table knows, plus one it does not.
    """
    script = (REPO_ROOT / "install.sh").read_text()
    helper = re.search(r"^engine_for_arch\(\) \{\n.*?^\}$", script, re.M | re.S)
    assert helper is not None, "install.sh no longer defines engine_for_arch()"

    arches = [*engine._ENGINE_BY_ARCH, "riscv64", "armv7l", "i686"]
    # One line per architecture: the command substitution turns "printed nothing"
    # into an empty line rather than no line at all.
    loop = f'for a in {" ".join(arches)}; do printf \'%s\\n\' "$(engine_for_arch "$a")"; done'
    result = subprocess.run(
        ["/bin/bash", "-c", f"{helper.group(0)}\n{loop}"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    answers = dict(zip(arches, result.stdout.splitlines(), strict=True))
    assert answers == {**engine._ENGINE_BY_ARCH, "riscv64": "", "armv7l": "", "i686": ""}


def test_a_scan_timeout_says_so_once_without_latching_the_engine_off(monkeypatch, tmp_path, capsys):
    # A timeout is not a property of the binary -- a smaller directory may well
    # finish -- so the engine stays in use. But a long wait followed by an even
    # slower pure-Python walk, said nothing about, leaves no way to tell a huge
    # tree from a stalled network mount.
    monkeypatch.setattr(platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(engine, "__file__", str(tmp_path / "engine.py"))
    (tmp_path / "bin").mkdir(exist_ok=True)
    (tmp_path / "bin" / "topo-core-x86_64").write_bytes(b"\x7fELF")
    monkeypatch.setattr(
        engine,
        "run_command",
        lambda *a, **k: CommandResult(args=list(a[0]), returncode=124, timed_out=True),
    )
    engine.ScanCache.clear()
    scan_path = engine.normalize_scan_path(tmp_path)

    assert engine.get_rust_scan_data(scan_path, use_cache=False) is None
    # Still usable: the next, smaller directory gets its chance.
    assert engine.engine_unusable_reason() is None
    assert "ran out of time" in capsys.readouterr().err

    # Said once per run, not once per root in a multi-root sweep.
    assert engine.get_rust_tree_data(scan_path) is None
    assert "ran out of time" not in capsys.readouterr().err


def test_a_schema_mismatch_latches_the_engine_off_for_the_rest_of_the_run(
    monkeypatch, tmp_path, capsys
):
    """One refusal is enough; the binary will not start speaking our schema.

    Before this, every caller kept forking an engine that could not answer --
    file_ops.get_size's fallback did it once per descendant directory, and each
    fork scanned a whole subtree before its result was discarded.
    """
    payload = _scan_payload()
    scan_path = _mock_engine_stdout(
        monkeypatch, tmp_path, _envelope(payload, version=engine.ENGINE_SCHEMA_VERSION + 1)
    )

    assert engine.get_rust_scan_data(scan_path, use_cache=False) is None
    assert engine.engine_unusable_reason() is not None
    # Said once, on stderr: a silent degradation to the slow path is undiagnosable.
    err = capsys.readouterr().err
    assert "scan engine is unusable" in err
    assert str(engine.ENGINE_SCHEMA_VERSION) in err

    # Latched: no later call reaches the subprocess, in either scan mode.
    monkeypatch.setattr(
        engine, "run_command", lambda *a, **k: pytest.fail("re-forked a latched-off engine")
    )
    assert engine.get_rust_scan_data(tmp_path / "other", use_cache=False) is None
    assert engine.get_rust_tree_data(tmp_path / "other") is None
    # And it does not repeat the notice on every later refusal.
    assert "scan engine is unusable" not in capsys.readouterr().err


def test_the_latch_still_serves_an_already_cached_measurement(monkeypatch, tmp_path):
    # The latch means "stop running the binary", not "forget what it measured
    # before it went bad": a cached entry is still a real measurement.
    scan_path = _mock_engine_stdout(
        monkeypatch, tmp_path, _envelope(_scan_payload(tmp_path, total_size_bytes=7))
    )
    assert engine.get_rust_scan_data(scan_path) is not None

    engine._mark_engine_unusable("test")
    monkeypatch.setattr(
        engine, "run_command", lambda *a, **k: pytest.fail("re-forked a latched-off engine")
    )
    cached = engine.get_rust_scan_data(scan_path)
    assert cached is not None
    assert cached["total_size_bytes"] == 7


def test_use_cache_false_does_not_disturb_the_cache(monkeypatch, tmp_path):
    # ScanCache.get costs a stat, moves the entry to the LRU tail and evicts it on
    # a moved signature. A caller that will not use the answer should not pay for
    # that, nor perturb the ordering the next eviction depends on.
    scan_path = _mock_engine_stdout(monkeypatch, tmp_path, _envelope(_scan_payload(tmp_path)))
    monkeypatch.setattr(
        engine.ScanCache, "get", lambda *a, **k: pytest.fail("looked up a cache it would not use")
    )

    assert engine.get_rust_scan_data(scan_path, use_cache=False) is not None
