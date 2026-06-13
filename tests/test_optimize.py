import os
import sqlite3
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.clean.optimize import (
    GPU_SHADER_CACHE_AGE_DAYS,
    run_autostart_cleanup,
    run_coredump_cleanup,
    run_desktop_database_refresh,
    run_gpu_shader_cache_cleanup,
    run_memory_opt,
    run_mime_database_refresh,
    run_systemd_user_service_cleanup,
    run_user_systemd_reset_failed,
    run_vacuum_all,
    vacuum_single_db,
)
from src.core.system import CommandResult


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
    mock_run.assert_called_once_with(
        ["systemctl", "--user", "daemon-reload"], capture=True, timeout=10
    )


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


def test_run_autostart_cleanup_removes_missing_absolute_exec(test_env):
    autostart_dir = test_env / ".config/autostart"
    autostart_dir.mkdir(parents=True)
    desktop_file = autostart_dir / "dead.desktop"
    desktop_file.write_text("[Desktop Entry]\nExec=/missing/dead-app --background\n")

    with patch("pathlib.Path.home", return_value=test_env):
        result = run_autostart_cleanup(dry_run=False)

    assert result == "Removed 1 zombie autostart entries"
    assert not desktop_file.exists()


def test_run_autostart_cleanup_dry_run_keeps_missing_exec_file(test_env):
    autostart_dir = test_env / ".config/autostart"
    autostart_dir.mkdir(parents=True)
    desktop_file = autostart_dir / "dead.desktop"
    desktop_file.write_text("[Desktop Entry]\nExec=/missing/dead-app\n")

    with patch("pathlib.Path.home", return_value=test_env):
        result = run_autostart_cleanup(dry_run=True)

    assert result == "Found 1 zombie autostart entries"
    assert desktop_file.exists()


def test_run_autostart_cleanup_keeps_quoted_existing_exec(test_env):
    app_dir = test_env / "Apps"
    app_dir.mkdir()
    app_path = app_dir / "My App"
    app_path.write_text("#!/bin/sh\n")
    autostart_dir = test_env / ".config/autostart"
    autostart_dir.mkdir(parents=True)
    desktop_file = autostart_dir / "valid.desktop"
    desktop_file.write_text(f'[Desktop Entry]\nExec="{app_path}" --background\n')

    with patch("pathlib.Path.home", return_value=test_env):
        result = run_autostart_cleanup(dry_run=False)

    assert result is None
    assert desktop_file.exists()


def test_run_autostart_cleanup_keeps_malformed_exec(test_env):
    autostart_dir = test_env / ".config/autostart"
    autostart_dir.mkdir(parents=True)
    desktop_file = autostart_dir / "malformed.desktop"
    desktop_file.write_text('[Desktop Entry]\nExec="/missing/dead-app\n')

    with patch("pathlib.Path.home", return_value=test_env):
        result = run_autostart_cleanup(dry_run=False)

    assert result is None
    assert desktop_file.exists()


def test_run_coredump_cleanup_skips_when_no_core_files(tmp_path):
    coredump_dir = tmp_path / "coredump"
    coredump_dir.mkdir()
    (coredump_dir / "note.txt").write_text("not a coredump")

    with (
        patch("src.clean.optimize.COREDUMP_DIR", coredump_dir),
        patch("src.clean.optimize.run_command") as mock_run,
    ):
        result = run_coredump_cleanup(dry_run=False)

    assert result is None
    mock_run.assert_not_called()


def test_run_coredump_cleanup_dry_run_keeps_core_files(tmp_path):
    coredump_dir = tmp_path / "coredump"
    coredump_dir.mkdir()
    core_file = coredump_dir / "core.app.1000"
    core_file.write_text("core")

    with (
        patch("src.clean.optimize.COREDUMP_DIR", coredump_dir),
        patch("src.clean.optimize.run_command") as mock_run,
    ):
        result = run_coredump_cleanup(dry_run=True)

    assert result == "System coredumps would be cleared"
    assert core_file.exists()
    mock_run.assert_not_called()


def test_run_coredump_cleanup_deletes_core_files_with_find(tmp_path):
    coredump_dir = tmp_path / "coredump"
    coredump_dir.mkdir()
    (coredump_dir / "core.app.1000").write_text("core")

    with (
        patch("src.clean.optimize.COREDUMP_DIR", coredump_dir),
        patch(
            "src.clean.optimize.run_command",
            return_value=CommandResult(["find"], 0),
        ) as mock_run,
    ):
        result = run_coredump_cleanup(dry_run=False)

    assert result == "System coredumps cleared"
    mock_run.assert_called_once_with(
        [
            "find",
            str(coredump_dir),
            "-maxdepth",
            "1",
            "-type",
            "f",
            "-name",
            "core.*",
            "-delete",
        ],
        use_sudo=True,
        capture=True,
    )


def test_run_coredump_cleanup_returns_none_when_find_fails(tmp_path):
    coredump_dir = tmp_path / "coredump"
    coredump_dir.mkdir()
    (coredump_dir / "core.app.1000").write_text("core")

    with (
        patch("src.clean.optimize.COREDUMP_DIR", coredump_dir),
        patch(
            "src.clean.optimize.run_command",
            return_value=CommandResult(["find"], 1),
        ),
    ):
        result = run_coredump_cleanup(dry_run=False)

    assert result is None


def test_run_gpu_shader_cache_cleanup_removes_only_stale_entries(test_env):
    cache_dir = test_env / ".cache/mesa_shader_cache"
    cache_dir.mkdir(parents=True)
    stale_file = cache_dir / "old-shader"
    active_file = cache_dir / "active-shader"
    stale_file.write_text("stale")
    active_file.write_text("active")
    stale_time = time.time() - ((GPU_SHADER_CACHE_AGE_DAYS + 1) * 86400)
    os.utime(stale_file, (stale_time, stale_time))

    with patch("pathlib.Path.home", return_value=test_env):
        result = run_gpu_shader_cache_cleanup(dry_run=False)

    assert result.startswith("Removed 1 stale GPU shader cache item(s)")
    assert not stale_file.exists()
    assert active_file.exists()


def test_run_gpu_shader_cache_cleanup_dry_run_keeps_stale_entries(test_env):
    cache_dir = test_env / ".nv/ComputeCache"
    cache_dir.mkdir(parents=True)
    stale_file = cache_dir / "old-kernel"
    stale_file.write_text("stale")
    stale_time = time.time() - ((GPU_SHADER_CACHE_AGE_DAYS + 1) * 86400)
    os.utime(stale_file, (stale_time, stale_time))

    with patch("pathlib.Path.home", return_value=test_env):
        result = run_gpu_shader_cache_cleanup(dry_run=True)

    assert result.startswith("Found 1 stale GPU shader cache item(s)")
    assert stale_file.exists()


def test_run_user_systemd_reset_failed_resets_failed_units():
    list_result = CommandResult(
        ["systemctl"],
        0,
        stdout=(
            "app.service loaded failed failed App Service\n"
            "sync.timer loaded failed failed Sync Timer\n"
        ),
    )

    with (
        patch("src.clean.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "src.clean.optimize.run_command",
            side_effect=[
                list_result,
                CommandResult(["systemctl"], 0),
            ],
        ) as mock_run,
    ):
        result = run_user_systemd_reset_failed(dry_run=False)

    assert result == "Reset 2 failed user systemd unit state(s)"
    assert mock_run.call_args_list[0].args[0] == [
        "systemctl",
        "--user",
        "list-units",
        "--state=failed",
        "--no-legend",
        "--no-pager",
        "--plain",
    ]
    assert mock_run.call_args_list[1].args[0] == ["systemctl", "--user", "reset-failed"]


def test_run_user_systemd_reset_failed_dry_run_does_not_reset():
    with (
        patch("src.clean.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "src.clean.optimize.run_command",
            return_value=CommandResult(
                ["systemctl"],
                0,
                stdout="app.service loaded failed failed App Service\n",
            ),
        ) as mock_run,
    ):
        result = run_user_systemd_reset_failed(dry_run=True)

    assert result == "Found 1 failed user systemd unit(s)"
    assert mock_run.call_count == 1


def test_run_user_systemd_reset_failed_skips_when_no_failed_units():
    with (
        patch("src.clean.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "src.clean.optimize.run_command",
            return_value=CommandResult(["systemctl"], 0, stdout=""),
        ) as mock_run,
    ):
        result = run_user_systemd_reset_failed(dry_run=False)

    assert result is None
    assert mock_run.call_count == 1


def test_run_vacuum_all_skips_when_browser_is_running(test_env):
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.optimize._is_any_process_running", return_value=True),
    ):
        result = run_vacuum_all(dry_run=False)

    assert result == "Brave, Chrome, Edge, Firefox running; database optimization skipped"


def test_run_memory_opt_skips_when_memory_pressure_is_low():
    with (
        patch(
            "src.clean.optimize._read_memory_pressure", return_value=(False, "72% memory available")
        ),
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


def test_vacuum_single_db_closes_connection_on_error(tmp_path):
    """A PRAGMA/VACUUM failure must not leak the sqlite connection."""
    db = tmp_path / "broken.db"
    db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 200)  # valid header, corrupt body

    fake_conn = MagicMock()
    fake_conn.cursor.return_value.execute.side_effect = sqlite3.Error("corrupt")

    with patch("src.clean.optimize.sqlite3.connect", return_value=fake_conn):
        result = vacuum_single_db(db)

    assert result == 0
    fake_conn.close.assert_called_once()
