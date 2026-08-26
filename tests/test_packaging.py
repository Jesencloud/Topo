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


def test_a_prerelease_tag_never_reaches_the_packaging_job():
    """A prerelease tag gets a source-only release (D3).

    `--iteration 1` makes v1.2.3-rc.1 into deb Version 1.2.3-rc.1-1, and dpkg
    reads everything after the last hyphen as the Debian revision, so that
    outranks the 1.2.3-1 that follows: the rc-to-release `apt install` is a
    refused downgrade, while topo's own packaging.version comparison puts the rc
    below the release. Not building the package at all is what closes the gap.
    """
    # Comments stripped: this file explains the gating in prose right next to it,
    # and prose that mentions `!cancelled()` must not stand in for the condition.
    workflow = "\n".join(
        line
        for line in (REPO_ROOT / ".github/workflows/release.yml").read_text().splitlines()
        if not line.lstrip().startswith("#")
    )

    # One classifier for the whole workflow. Two copies drifting apart is the
    # shape of D3 itself, so the count is the assertion.
    classifiers = [line for line in workflow.splitlines() if "contains(github.ref_name" in line]
    assert len(classifiers) == 1
    assert classifiers[0].strip().startswith("IS_PRERELEASE:")
    assert "prerelease: ${{ steps.classify.outputs.prerelease }}" in workflow
    assert "if: needs.tag-kind.outputs.prerelease != 'true'" in workflow
    assert "prerelease: ${{ needs.tag-kind.outputs.prerelease == 'true' }}" in workflow

    # Skipping `package` skips the smoke jobs that need it, which would take the
    # release job with them under the default `success()`; !cancelled() lifts that,
    # and on its own it would publish over a failed smoke test -- or over a failed
    # `build`, which arrives here as a skipped `package` rather than a failure.
    assert "!cancelled()" in workflow
    assert "!contains(needs.*.result, 'failure')" in workflow
    assert "needs.build.result == 'success'" in workflow

    # Nothing downstream may assume the packages exist: the artifact download,
    # the staging copy, the checksum manifest and the attached asset list.
    assert "if: env.PRERELEASE != 'true'" in workflow
    for guarded in (
        "cp dist/packages/*.deb release-assets/",
        "assets+=(*.deb *.rpm)",
        "printf '%s\\n' release-assets/*.deb release-assets/*.rpm",
    ):
        prefix = workflow.split(guarded)[0]
        assert prefix.rsplit("\n", 2)[-2].strip() == 'if [ "$PRERELEASE" != true ]; then'
    assert "\n            release-assets/*.deb\n" not in workflow


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
