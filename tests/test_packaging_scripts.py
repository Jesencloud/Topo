import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _packaging_script_env(tmp_path: Path, home: Path) -> dict[str, str]:
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    _write_executable(
        fake_bin / "getent",
        """#!/bin/sh
if [ "$1" = "passwd" ]; then
    case "$2" in
        alice|1000)
            printf 'alice:x:1000:1000:Alice:%s:/bin/sh\\n' "$TOPO_TEST_HOME"
            exit 0
            ;;
    esac
fi
exit 2
""",
    )
    _write_executable(
        fake_bin / "id",
        """#!/bin/sh
if [ "$1" = "-gn" ]; then
    echo alice
    exit 0
fi
if [ "$1" = "-u" ] && [ -n "${TOPO_TEST_FAKE_UID:-}" ]; then
    echo "$TOPO_TEST_FAKE_UID"
    exit 0
fi
exec /usr/bin/id "$@"
""",
    )
    # Stands in for util-linux runuser: records the argv it was handed, then runs
    # the command unchanged so the deprivileged path can still be inspected.
    _write_executable(
        fake_bin / "runuser",
        """#!/bin/sh
printf '%s\\n' "$*" >> "$TOPO_TEST_RUNUSER_LOG"
shift 2
if [ "$1" = "--" ]; then
    shift
fi
exec "$@"
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["SUDO_USER"] = "alice"
    env["TOPO_TEST_HOME"] = str(home)
    env.pop("SUDO_UID", None)
    return env


def test_package_after_install_replaces_stale_script_symlink(tmp_path):
    home = tmp_path / "home"
    script_install = home / ".topo"
    user_bin = home / ".local" / "bin"
    script_install.mkdir(parents=True)
    user_bin.mkdir(parents=True)
    (script_install / "topo").write_text("#!/bin/sh\n")
    stale_launcher = user_bin / "topo"
    stale_launcher.symlink_to(script_install / "topo")

    subprocess.run(
        [str(REPO_ROOT / "packaging/scripts/after-install.sh")],
        env=_packaging_script_env(tmp_path, home),
        check=True,
    )

    assert stale_launcher.is_file()
    assert not stale_launcher.is_symlink()
    assert "Managed by topo package compatibility launcher" in stale_launcher.read_text()
    assert "/usr/bin/topo" in stale_launcher.read_text()


def test_package_after_install_preserves_user_regular_file(tmp_path):
    home = tmp_path / "home"
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    launcher = user_bin / "topo"
    launcher.write_text("#!/bin/sh\necho custom\n")

    subprocess.run(
        [str(REPO_ROOT / "packaging/scripts/after-install.sh")],
        env=_packaging_script_env(tmp_path, home),
        check=True,
    )

    assert launcher.read_text() == "#!/bin/sh\necho custom\n"


def test_package_after_install_preserves_foreign_user_symlink(tmp_path):
    """A link to the user's own build is not a stale Topo link and must survive."""
    home = tmp_path / "home"
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    own_build = tmp_path / "mybuild" / "topo"
    own_build.parent.mkdir(parents=True)
    own_build.write_text("#!/bin/sh\necho mine\n")
    launcher = user_bin / "topo"
    launcher.symlink_to(own_build)

    subprocess.run(
        [str(REPO_ROOT / "packaging/scripts/after-install.sh")],
        env=_packaging_script_env(tmp_path, home),
        check=True,
    )

    assert launcher.is_symlink()
    assert launcher.resolve() == own_build.resolve()


def test_package_after_install_skips_symlinked_bin_dir(tmp_path):
    """A symlinked ~/.local/bin must not be followed into a system directory."""
    home = tmp_path / "home"
    (home / ".local").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (home / ".local" / "bin").symlink_to(elsewhere)

    result = subprocess.run(
        [str(REPO_ROOT / "packaging/scripts/after-install.sh")],
        env=_packaging_script_env(tmp_path, home),
        capture_output=True,
        text=True,
        check=True,
    )

    assert "is a symlink" in result.stderr
    assert not (elsewhere / "topo").exists()
    assert list(elsewhere.iterdir()) == []


def test_package_after_install_skips_non_directory_bin_dir(tmp_path):
    """Any non-directory at ~/.local/bin is left untouched."""
    home = tmp_path / "home"
    (home / ".local").mkdir(parents=True)
    blocker = home / ".local" / "bin"
    blocker.write_text("not a directory")

    result = subprocess.run(
        [str(REPO_ROOT / "packaging/scripts/after-install.sh")],
        env=_packaging_script_env(tmp_path, home),
        capture_output=True,
        text=True,
        check=True,
    )

    assert "is not a directory" in result.stderr
    assert blocker.read_text() == "not a directory"


def test_package_after_install_deprivileges_write_when_root(tmp_path):
    """As root the launcher is written through runuser, never by root itself."""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    runuser_log = tmp_path / "runuser.log"
    env = _packaging_script_env(tmp_path, home)
    env["TOPO_TEST_FAKE_UID"] = "0"
    env["TOPO_TEST_RUNUSER_LOG"] = str(runuser_log)

    subprocess.run(
        [str(REPO_ROOT / "packaging/scripts/after-install.sh")],
        env=env,
        check=True,
    )

    invocation = runuser_log.read_text()
    assert invocation.startswith("-u alice --")
    launcher = home / ".local" / "bin" / "topo"
    assert launcher.is_file() and not launcher.is_symlink()
    assert launcher.stat().st_mode & 0o777 == 0o755
    assert "Managed by topo package compatibility launcher" in launcher.read_text()


def test_package_after_install_skips_when_runuser_fails(tmp_path):
    """If the deprivileged write cannot run, root writes nothing and exits clean."""
    home = tmp_path / "home"
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    env = _packaging_script_env(tmp_path, home)
    env["TOPO_TEST_FAKE_UID"] = "0"
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    _write_executable(
        fake_bin / "runuser",
        """#!/bin/sh
echo "runuser: refused" >&2
exit 1
""",
    )

    result = subprocess.run(
        [str(REPO_ROOT / "packaging/scripts/after-install.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.returncode == 0
    assert not (user_bin / "topo").exists()


def test_package_after_install_skips_when_runuser_missing(tmp_path):
    """Without runuser the launcher is skipped rather than written as root."""
    home = tmp_path / "home"
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    env = _packaging_script_env(tmp_path, home)
    env["TOPO_TEST_FAKE_UID"] = "0"
    fake_bin = Path(env["PATH"].split(os.pathsep)[0])
    (fake_bin / "runuser").unlink()
    # A PATH that provably contains no runuser, only the tools the script needs.
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("sh", "cut", "readlink", "grep", "mkdir", "rm", "cat", "chmod", "mv"):
        resolved = shutil.which(name)
        assert resolved, name
        (tools / name).symlink_to(resolved)
    env["PATH"] = f"{fake_bin}{os.pathsep}{tools}"

    result = subprocess.run(
        [str(REPO_ROOT / "packaging/scripts/after-install.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "runuser is unavailable" in result.stderr
    assert not (user_bin / "topo").exists()


def test_package_after_remove_removes_managed_launcher_without_script_install(tmp_path):
    home = tmp_path / "home"
    app_dir = tmp_path / "usr/lib/topo"
    (app_dir / "src/core/bin").mkdir(parents=True)
    user_bin = home / ".local" / "bin"
    user_bin.mkdir(parents=True)
    launcher = user_bin / "topo"
    launcher.write_text("#!/bin/sh\n# Managed by topo package compatibility launcher.\n")
    env = _packaging_script_env(tmp_path, home)
    env["TOPO_PACKAGE_APP_DIR"] = str(app_dir)

    subprocess.run(
        [str(REPO_ROOT / "packaging/scripts/after-remove.sh")],
        env=env,
        check=True,
    )

    assert not launcher.exists()
    assert not app_dir.exists()


def test_package_after_remove_keeps_launcher_when_script_install_exists(tmp_path):
    home = tmp_path / "home"
    script_install = home / ".topo"
    user_bin = home / ".local" / "bin"
    script_install.mkdir(parents=True)
    user_bin.mkdir(parents=True)
    script_topo = script_install / "topo"
    script_topo.write_text("#!/bin/sh\n")
    script_topo.chmod(0o755)
    launcher = user_bin / "topo"
    launcher.write_text("#!/bin/sh\n# Managed by topo package compatibility launcher.\n")

    subprocess.run(
        [str(REPO_ROOT / "packaging/scripts/after-remove.sh")],
        env=_packaging_script_env(tmp_path, home),
        check=True,
    )

    assert launcher.exists()
