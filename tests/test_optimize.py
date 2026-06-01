from pathlib import Path
from unittest.mock import patch

from src.clean.optimize import (
    run_desktop_database_refresh,
    run_memory_opt,
    run_mime_database_refresh,
    run_systemd_user_service_cleanup,
    run_vacuum_all,
)


def test_run_systemd_user_service_cleanup_removes_broken_unit(test_env):
    service_dir = test_env / ".config/systemd/user"
    service_dir.mkdir(parents=True)
    service_file = service_dir / "dead-app.service"
    service_file.write_text("[Service]\nExecStart=/missing/dead-app\n")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch("src.clean.optimize.run_command") as mock_run,
    ):
        result = run_systemd_user_service_cleanup(dry_run=False)

    assert result == "Removed 1 broken user systemd service(s)"
    assert not service_file.exists()
    mock_run.assert_called_once_with(["systemctl", "--user", "daemon-reload"], capture=True, timeout=10)


def test_run_systemd_user_service_cleanup_keeps_valid_unit(test_env):
    service_dir = test_env / ".config/systemd/user"
    service_dir.mkdir(parents=True)
    service_file = service_dir / "valid.service"
    service_file.write_text(f"[Service]\nExecStart={Path('/bin/sh')}\n")

    with patch("pathlib.Path.home", return_value=test_env):
        result = run_systemd_user_service_cleanup(dry_run=False)

    assert result is None
    assert service_file.exists()


def test_run_systemd_user_service_cleanup_dry_run_keeps_file(test_env):
    service_dir = test_env / ".config/systemd/user"
    service_dir.mkdir(parents=True)
    service_file = service_dir / "dead-app.service"
    service_file.write_text("[Service]\nExecStart=/missing/dead-app\n")

    with patch("pathlib.Path.home", return_value=test_env):
        result = run_systemd_user_service_cleanup(dry_run=True)

    assert result == "Found 1 broken user systemd service(s)"
    assert service_file.exists()


def test_run_vacuum_all_skips_when_browser_is_running(test_env):
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.optimize._is_any_process_running", return_value=True),
    ):
        result = run_vacuum_all(dry_run=False)

    assert result == "Brave, Chrome, Edge, Firefox running; database optimization skipped"


def test_run_memory_opt_skips_when_memory_pressure_is_low():
    with (
        patch("src.clean.optimize._read_memory_pressure", return_value=(False, "72% memory available")),
        patch("src.clean.optimize.has_sudo") as mock_has_sudo,
        patch("src.clean.optimize.run_command") as mock_run,
    ):
        result = run_memory_opt()

    assert result == "Memory pressure already optimal (72% memory available)"
    mock_has_sudo.assert_not_called()
    mock_run.assert_not_called()


def test_run_desktop_database_refresh_dry_run(test_env):
    app_dir = test_env / ".local/share/applications"
    app_dir.mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.optimize.shutil.which", return_value="/usr/bin/update-desktop-database"),
    ):
        result = run_desktop_database_refresh(dry_run=True)

    assert result == "Desktop application database would be refreshed"


def test_run_mime_database_refresh_dry_run(test_env):
    mime_dir = test_env / ".local/share/mime"
    mime_dir.mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.optimize.shutil.which", return_value="/usr/bin/update-mime-database"),
    ):
        result = run_mime_database_refresh(dry_run=True)

    assert result == "MIME database would be refreshed"
