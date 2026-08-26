import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_FPM_STUB = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$(mktemp "$FPM_ARGV_DIR/argv.XXXXXX")"
"""


def _fpm_calls(tmp_path):
    """Run the build script with fpm stubbed out, one record per invocation.

    The stub only writes down its argv, so nothing is packaged and neither fpm
    nor ruby has to be installed to run this test. Both engines are passed
    explicitly so the run does not depend on which topo-core binaries happen to
    be sitting in the checkout.
    """
    argv_dir = tmp_path / "argv"
    bin_dir = tmp_path / "bin"
    for directory in (argv_dir, bin_dir):
        directory.mkdir()
    stub = bin_dir / "fpm"
    stub.write_text(_FPM_STUB)
    stub.chmod(0o755)
    engine = tmp_path / "topo-core-fake"
    engine.write_bytes(b"")

    subprocess.run(
        [
            "bash",
            str(REPO_ROOT / "packaging/build-linux-packages.sh"),
            "--version",
            "9.9.9",
            "--output-dir",
            str(tmp_path / "out"),
            "--x86_64-engine",
            str(engine),
            "--aarch64-engine",
            str(engine),
        ],
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "FPM_ARGV_DIR": str(argv_dir),
        },
    )

    calls = []
    for argv_file in sorted(argv_dir.iterdir()):
        argv = argv_file.read_text().splitlines()
        package_type = argv[argv.index("-t") + 1]
        depends = [argv[i + 1] for i, token in enumerate(argv) if token == "--depends"]
        calls.append((package_type, tuple(depends)))
    return calls


def test_system_packages_do_not_modify_user_home_from_maintainer_scripts():
    build_script = (REPO_ROOT / "packaging/build-linux-packages.sh").read_text()

    assert "--after-install" not in build_script
    assert "--after-remove" not in build_script
    assert not (REPO_ROOT / "packaging/scripts/after-install.sh").exists()
    assert not (REPO_ROOT / "packaging/scripts/after-remove.sh").exists()


def test_only_the_deb_bounds_python_at_310(tmp_path):
    """The interpreter floor belongs in the deb's metadata, not the rpm's (D5).

    topo needs 3.10, so on Debian 11 (3.9) and Ubuntu 20.04 (3.8) an unversioned
    `python3` dependency let the install succeed and left the launcher to refuse
    on first run. With the bound, apt says "Depends: python3 (>= 3.10) but
    3.9.2-3 is to be installed" and unpacks nothing.

    The rpm cannot carry the same bound: openSUSE Leap 15.6's python3 is 3.6 and
    the release workflow supplies 3.11 as python311 plus a symlink, which rpm's
    dependency solver cannot see.
    """
    calls = _fpm_calls(tmp_path)

    # Both architectures, and the two package types differ in this one dependency.
    assert len(calls) == 4
    assert sorted(set(calls)) == [
        ("deb", ("curl", "python3 (>= 3.10)", "python3-packaging")),
        ("rpm", ("curl", "python3", "python3-packaging")),
    ]
