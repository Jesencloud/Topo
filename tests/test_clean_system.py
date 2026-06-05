from unittest.mock import MagicMock, patch

from src.clean.system import (
    clean_journal,
    clean_orphaned_packages,
    clean_package_manager,
    clean_snaps,
    clean_zombies,
)


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_orphaned_packages_fedora(mock_get_os_id, mock_run, mock_which):
    mock_get_os_id.return_value = "fedora"
    mock_which.return_value = "/usr/bin/dnf"
    mock_run.return_value = MagicMock(returncode=0, stdout="Removed 500 MB\nPackage1\nPackage2")

    s, i, c = clean_orphaned_packages(dry_run=False)
    assert s > 0
    assert i > 0
    assert c == 1
    mock_run.assert_called_with(["dnf", "autoremove", "-y"], use_sudo=True, capture=True)

    s, i, c = clean_orphaned_packages(dry_run=True)
    assert c == 1


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_orphaned_packages_ubuntu(mock_get_os_id, mock_run, mock_which):
    mock_get_os_id.return_value = "ubuntu"
    mock_which.return_value = "/usr/bin/apt-get"
    mock_run.return_value = MagicMock(
        returncode=0, stdout="After this operation, 50.0 MB of additional disk space will be freed."
    )

    s, i, c = clean_orphaned_packages(dry_run=False)
    assert s > 0
    assert i == 1
    assert c == 1
    mock_run.assert_called_with(["apt-get", "autoremove", "-y"], use_sudo=True, capture=True)


@patch("src.clean.system.run_command")
def test_clean_zombies(mock_run):
    # Mock ps output with a zombie process
    mock_run.return_value = MagicMock(
        returncode=0,
        ok=True,
        stdout="S   PID  PPID COMMAND\nZ   1234  1111 defunct-app\nS   5678  2222 normal-app\n",
    )

    s, i, c = clean_zombies(dry_run=False)
    assert i == 1
    assert c == 1
    # Check that SIGCHLD was sent to the parent (1111)
    mock_run.assert_any_call(["kill", "-SIGCHLD", "1111"], use_sudo=True, capture=True)

    # Test dry run
    s, i, c = clean_zombies(dry_run=True)
    assert c == 1


@patch("shutil.which")
@patch("src.clean.system.run_command")
def test_clean_snaps(mock_run, mock_which):
    mock_which.return_value = "/usr/bin/snap"
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="Name  Version  Rev   Tracking  Publisher   Notes\ncore22  2023  1234  latest    canonical*  disabled\n",
    )

    s, i, c = clean_snaps(dry_run=False)
    assert i == 1
    assert c == 1
    mock_run.assert_any_call(
        ["snap", "remove", "core22", "--revision", "1234"], use_sudo=True, capture=True
    )

    s, i, c = clean_snaps(dry_run=True)
    assert c == 1


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_package_manager_fedora(mock_get_os_id, mock_run, mock_which):
    mock_get_os_id.return_value = "fedora"
    mock_which.return_value = "/usr/bin/dnf"
    mock_run.return_value = MagicMock(returncode=0, stdout="freed 100 MB")

    s, i, c = clean_package_manager(dry_run=False)
    assert s > 0
    assert i == 1
    assert c == 1
    mock_run.assert_called_with(["dnf", "clean", "all"], use_sudo=True, capture=True)

    s, i, c = clean_package_manager(dry_run=True)
    assert c == 1


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_package_manager_ubuntu(mock_get_os_id, mock_run, mock_which):
    mock_get_os_id.return_value = "ubuntu"
    mock_which.side_effect = lambda x: "/usr/bin/apt-get" if x == "apt-get" else None
    mock_run.return_value = MagicMock(returncode=0, stdout="")

    s, i, c = clean_package_manager(dry_run=False)
    assert i == 1
    assert c == 1
    mock_run.assert_any_call(["apt-get", "clean"], use_sudo=True, capture=True)


@patch("shutil.which")
@patch("src.clean.system.run_command")
def test_clean_journal(mock_run, mock_which):
    mock_which.return_value = "/usr/bin/journalctl"
    mock_run.return_value = MagicMock(returncode=0, stdout="freed 200 MB")

    s, i, c = clean_journal(dry_run=False)
    assert s > 0
    assert i == 1
    assert c == 1
    mock_run.assert_called_with(["journalctl", "--vacuum-size=1M"], use_sudo=True, capture=True)

    s, i, c = clean_journal(dry_run=True)
    assert c == 1


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_package_manager_ubuntu_includes_snap_stats(mock_get_os_id, mock_run, mock_which):
    """Snap revision removals must be counted in package-manager stats."""
    mock_get_os_id.return_value = "ubuntu"
    mock_which.side_effect = lambda x: f"/usr/bin/{x}" if x in ("apt-get", "snap") else None

    def run_side_effect(cmd, **kwargs):
        if cmd[:2] == ["snap", "list"]:
            return MagicMock(
                returncode=0,
                ok=True,
                stdout="Name Version Rev Tracking Publisher Notes\n"
                "core22 2023 1234 latest canonical* disabled\n",
            )
        return MagicMock(returncode=0, ok=True, stdout="")

    mock_run.side_effect = run_side_effect

    s, i, c = clean_package_manager(dry_run=False)
    # apt cache (1 item / 1 cat) + one removed snap revision (1 item / 1 cat)
    assert i == 2
    assert c == 2
