import os
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
exec /usr/bin/id "$@"
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
