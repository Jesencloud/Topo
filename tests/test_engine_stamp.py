"""The gate that keeps src/core/bin/ honest about the Rust source next to it.

A git or `--minimal` install runs the two engines committed under src/core/bin/,
and nothing used to check that they were built from the topo-core/ sources in the
same commit. `packaging/build-engine.sh --verify` is that check, and check.sh
step 4 -- which used to *overwrite* the tracked musl binary with a glibc-dynamic
host build -- is now one of its callers.

The tests run the real script over a copy of the checkout, so a tampered source
file or a swapped binary is a real tamper rather than a mocked one.
"""

import platform
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BUILD_ENGINE = REPO_ROOT / "packaging/build-engine.sh"
STAMP = REPO_ROOT / "topo-core/engine.stamp"
ENGINE_ARCHES = ("x86_64", "aarch64")
REBUILD_HINT = "请重新构建并提交引擎"

# readelf reads any architecture, but running one -- and standing /bin/true in
# for a host build -- only works where the host is the architecture in question.
on_x86_64 = pytest.mark.skipif(
    platform.machine() != "x86_64", reason="needs an x86_64 host to run the x86_64 engine"
)


def _run(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _copy_checkout(tmp_path: Path) -> Path:
    """A tree with just enough of the repo for the script to answer.

    The script locates everything relative to its own directory, so a copy is a
    complete world -- and tampering there cannot disturb the real checkout.
    """
    root = tmp_path / "repo"
    (root / "packaging").mkdir(parents=True)
    shutil.copy2(BUILD_ENGINE, root / "packaging/build-engine.sh")
    shutil.copytree(REPO_ROOT / "topo-core/src", root / "topo-core/src")
    for name in ("Cargo.toml", "Cargo.lock", "engine.stamp"):
        shutil.copy2(REPO_ROOT / "topo-core" / name, root / "topo-core" / name)
    (root / "src/core/bin").mkdir(parents=True)
    for arch in ENGINE_ARCHES:
        shutil.copy2(
            REPO_ROOT / "src/core/bin" / f"topo-core-{arch}",
            root / "src/core/bin" / f"topo-core-{arch}",
        )
    return root


def test_the_checked_in_engines_match_the_checked_in_stamp():
    """The one assertion that fails on a real commit of stale binaries."""
    result = _run(BUILD_ENGINE, "--verify")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "match topo-core/engine.stamp" in result.stdout


def test_the_stamp_records_a_hash_for_the_source_and_for_both_engines():
    keys = [line.split()[0] for line in STAMP.read_text().splitlines() if not line.startswith("#")]
    assert keys == ["source", "topo-core-x86_64", "topo-core-aarch64"]


def test_editing_the_rust_source_fails_verification(tmp_path):
    root = _copy_checkout(tmp_path)
    scanner = root / "topo-core/src/scanner.rs"
    scanner.write_text(scanner.read_text() + "\n// a change nobody rebuilt for\n")

    result = _run(root / "packaging/build-engine.sh", "--verify")
    assert result.returncode == 1
    assert "topo-core/ has changed" in result.stderr
    assert REBUILD_HINT in result.stderr


def test_swapping_a_bundled_engine_fails_verification(tmp_path):
    root = _copy_checkout(tmp_path)
    engine = root / "src/core/bin/topo-core-x86_64"
    engine.write_bytes(engine.read_bytes() + b"\x00")

    result = _run(root / "packaging/build-engine.sh", "--verify")
    assert result.returncode == 1
    assert "is not the binary the stamp was written for" in result.stderr
    assert REBUILD_HINT in result.stderr


def test_a_missing_engine_or_stamp_fails_verification(tmp_path):
    root = _copy_checkout(tmp_path)
    (root / "src/core/bin/topo-core-aarch64").unlink()
    result = _run(root / "packaging/build-engine.sh", "--verify")
    assert result.returncode == 1
    assert "is missing" in result.stderr

    (root / "topo-core/engine.stamp").unlink()
    result = _run(root / "packaging/build-engine.sh", "--verify")
    assert result.returncode == 1
    assert "missing or has no source hash" in result.stderr


@on_x86_64
def test_a_glibc_dynamic_engine_fails_verification(tmp_path):
    """The accident this whole gate exists for: a host build copied into place.

    /bin/true stands in for it -- same architecture, dynamically linked. Its
    hash is written into the stamp first, so the failure has to come from the ELF
    properties and not from the hash that already caught it.
    """
    root = _copy_checkout(tmp_path)
    host_build = Path("/bin/true")
    engine = root / "src/core/bin/topo-core-x86_64"
    shutil.copy2(host_build, engine)
    stamp = root / "topo-core/engine.stamp"
    digest = subprocess.run(
        ["sha256sum", str(engine)], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    stamp.write_text(
        "\n".join(
            f"topo-core-x86_64  {digest}" if line.startswith("topo-core-x86_64") else line
            for line in stamp.read_text().splitlines()
        )
        + "\n"
    )

    result = _run(root / "packaging/build-engine.sh", "--verify")
    assert result.returncode == 1
    assert "not statically linked" in result.stderr


@on_x86_64
def test_compare_diffs_the_bundled_engine_against_a_fresh_build():
    """No cargo in the test environment, so the bundled engine plays both parts.

    That still exercises everything CI depends on: the fixture tree, the three
    scan modes, and the JSON normalisation without which two runs of the *same*
    binary disagree (subdirs is a HashMap, and --stats folds in atime).
    """
    engine = REPO_ROOT / "src/core/bin/topo-core-x86_64"
    result = _run(BUILD_ENGINE, "--compare", str(engine), "x86_64")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "matches a fresh build" in result.stdout


def test_compare_rejects_a_binary_for_the_other_architecture():
    engine = REPO_ROOT / "src/core/bin/topo-core-aarch64"
    result = _run(BUILD_ENGINE, "--compare", str(engine), "x86_64")
    assert result.returncode == 1
    assert "not an x86_64 ELF binary" in result.stderr


def test_check_elf_accepts_a_bundled_engine_and_rejects_a_dynamic_one():
    """The mode both workflows call instead of keeping their own readelf block."""
    engine = REPO_ROOT / "src/core/bin/topo-core-aarch64"
    result = _run(BUILD_ENGINE, "--check-elf", str(engine), "aarch64")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "statically linked aarch64 ELF binary" in result.stdout

    result = _run(BUILD_ENGINE, "--check-elf", "/bin/true", "x86_64")
    assert result.returncode == 1
    assert "not statically linked" in result.stderr

    assert _run(BUILD_ENGINE, "--check-elf", "/bin/true").returncode == 1


def test_the_script_refuses_arguments_it_cannot_act_on(tmp_path):
    missing = tmp_path / "nowhere"
    assert _run(BUILD_ENGINE, "--verify", "extra").returncode == 1
    assert _run(BUILD_ENGINE, "--compare", str(missing)).returncode == 1
    assert (
        "not an executable file" in _run(BUILD_ENGINE, "--compare", str(missing), "x86_64").stderr
    )
    assert _run(BUILD_ENGINE, "--nonsense").returncode == 1
    assert "Usage:" in _run(BUILD_ENGINE, "--help").stdout


def test_check_sh_verifies_the_bundled_engine_instead_of_overwriting_it():
    """Step 4 used to build on the host and `mv` the result over the tracked
    musl binary, leaving a glibc-dynamic engine that only works on this box."""
    check = (REPO_ROOT / "check.sh").read_text()
    assert "packaging/build-engine.sh --verify" in check
    assert "src/core/bin/topo-core-${ENGINE_ARCH}" not in check
    assert "cargo build --quiet --release --manifest-path topo-core/Cargo.toml" not in check


def test_both_workflows_gate_the_bundled_engine():
    for name in ("build-engine.yml", "release.yml"):
        workflow = (REPO_ROOT / ".github/workflows" / name).read_text()
        assert "packaging/build-engine.sh --verify" in workflow
        assert 'packaging/build-engine.sh --compare "topo-core-x86_64" x86_64' in workflow
        # The ELF assertions have one owner now; a second copy here is what used
        # to drift (no LC_ALL=C, and one of the checks written as `grep -q` while
        # the others printed their own error).
        assert 'packaging/build-engine.sh --check-elf "$BIN"' in workflow
        assert "readelf" not in workflow
    # Without this path, an engine-only commit never reaches the gate.
    assert '- "src/core/bin/**"' in (REPO_ROOT / ".github/workflows/build-engine.yml").read_text()
