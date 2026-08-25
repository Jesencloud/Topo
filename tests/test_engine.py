"""Which machines get a Rust engine, and what the rest get instead.

`get_core_binary()` used to answer with a name it derived by elimination --
aarch64 for the two spellings of it, x86_64 for *everything else*. So riscv64,
armv7l and i686 were handed the x86_64 engine that the source archive carries,
and paid an `Exec format error` per scan instead of falling back to the
pure-Python path the callers already have for a missing engine.

install.sh reaches the same conclusion from the same list, and the tests here
require the two to keep naming the same things.
"""

import platform
import re
import subprocess
from pathlib import Path

import pytest

from src.core import engine

REPO_ROOT = Path(__file__).resolve().parents[1]


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
