import inspect
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.clean.optimize import (
    OptimizationRegistry,
    _points_at_transient_mount,
    run_autostart_cleanup,
    run_broken_symlink_cleanup,
    run_coredump_cleanup,
    run_desktop_database_refresh,
    run_mime_database_refresh,
    run_sysctl_optimize,
    run_systemd_user_service_cleanup,
    run_tmpfiles_cleanup,
    run_user_systemd_reset_failed,
    run_vacuum_all,
    vacuum_single_db,
)
from src.core.system import CommandResult


def test_registered_optimization_tasks_are_runnable_entry_points():
    """A decorator that slides onto a helper silently unregisters the real task.

    optimize_system() submits every registered callable as task(dry_run=...), so a
    helper caught by @register_optimization_task raises TypeError into the pool's
    swallowed exceptions while its own task vanishes from the run.
    """
    tasks = OptimizationRegistry.tasks
    names = {task.__name__ for task in tasks}
    assert "run_broken_symlink_cleanup" in names
    for task in tasks:
        assert task.__name__.startswith("run_"), task.__name__
        assert "dry_run" in inspect.signature(task).parameters, task.__name__


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

    # A working trash backend, so the entry goes somewhere recoverable.
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.core.file_ops._which_cached", return_value="/usr/bin/gio"),
        patch("src.core.file_ops.run_command", return_value=SimpleNamespace(ok=True)),
    ):
        result = run_autostart_cleanup(dry_run=False)

    assert result == "Removed 1 zombie autostart entries"


def test_run_autostart_cleanup_keeps_entry_without_trash_backend(test_env):
    """No trash backend means the entry is kept, never deleted unrecoverably (M-1)."""
    autostart_dir = test_env / ".config/autostart"
    autostart_dir.mkdir(parents=True)
    desktop_file = autostart_dir / "dead.desktop"
    desktop_file.write_text("[Desktop Entry]\nExec=/missing/dead-app --background\n")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.core.file_ops._which_cached", return_value=None),
    ):
        result = run_autostart_cleanup(dry_run=False)

    assert result == "Kept 1 zombie autostart entries (no trash backend available)"
    assert desktop_file.exists()


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


def _fake_trash(cmd, **kwargs):
    """Stand in for a working `gio trash`: the entry goes somewhere recoverable."""
    Path(cmd[-1]).unlink()
    return SimpleNamespace(ok=True)


def test_run_broken_symlink_cleanup_removes_only_broken_links(test_env):
    bin_dir = test_env / ".local/bin"
    bin_dir.mkdir(parents=True)
    target = test_env / "real-tool"
    target.write_text("#!/bin/sh\n")
    valid_link = bin_dir / "valid-tool"
    valid_link.symlink_to(target)
    broken_link = bin_dir / "missing-tool"
    broken_link.symlink_to(test_env / "missing-tool-target")
    regular_file = bin_dir / "regular-file"
    regular_file.write_text("keep")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.core.file_ops._which_cached", return_value="/usr/bin/gio"),
        patch("src.core.file_ops.run_command", side_effect=_fake_trash),
    ):
        result = run_broken_symlink_cleanup(dry_run=False)

    assert result == "Removed 1 broken user symlink(s)"
    assert not broken_link.exists()
    assert not broken_link.is_symlink()
    assert valid_link.exists()
    assert valid_link.is_symlink()
    assert regular_file.exists()


def test_run_broken_symlink_cleanup_keeps_link_without_trash_backend(test_env):
    """A dangling link is the last record of its target; never delete it unrecoverably (L-1)."""
    bin_dir = test_env / ".local/bin"
    bin_dir.mkdir(parents=True)
    broken_link = bin_dir / "missing-tool"
    broken_link.symlink_to(test_env / "missing-tool-target")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.core.file_ops._which_cached", return_value=None),
    ):
        result = run_broken_symlink_cleanup(dry_run=False)

    assert result == "Kept 1 broken user symlink(s) (no trash backend available)"
    assert broken_link.is_symlink()


def test_run_broken_symlink_cleanup_skips_relative_link_into_removable_mount(test_env):
    """The mount guard must normalize relative targets before matching (L-1)."""
    bin_dir = test_env / ".local/bin"
    bin_dir.mkdir(parents=True)
    link = bin_dir / "usb-tool"
    up = "../" * (len(bin_dir.parts) - 1)
    link.symlink_to(f"{up}media/usb-stick/tool")

    with patch("pathlib.Path.home", return_value=test_env):
        result = run_broken_symlink_cleanup(dry_run=True)

    assert result is None
    assert link.is_symlink()


def test_points_at_transient_mount_covers_gvfs_and_runtime_dirs(test_env):
    bin_dir = test_env / ".local/bin"
    bin_dir.mkdir(parents=True)

    with patch("pathlib.Path.home", return_value=test_env):
        for target in [
            "/run/user/1000/gvfs/smb-share:server=nas/file",
            "/run/user/1000/doc/1234/file",
            f"{test_env}/.gvfs",
            f"{test_env}/.gvfs/sftp-share/file",
            "/media/usb/file",
            "/mnt/backup/file",
            "/run/media/user/disk/file",
        ]:
            link = bin_dir / "probe"
            link.symlink_to(target)
            try:
                assert _points_at_transient_mount(link) is True, target
            finally:
                link.unlink()

        link = bin_dir / "probe"
        link.symlink_to(test_env / "really-gone")
        assert _points_at_transient_mount(link) is False


def test_run_broken_symlink_cleanup_skips_user_dirs_unless_opted_in(test_env, monkeypatch):
    """~/Desktop and ~/Documents hold hand-made links; scanning them is opt-in (L-1)."""
    desktop_dir = test_env / "Desktop"
    desktop_dir.mkdir(parents=True)
    broken_link = desktop_dir / "missing-app"
    broken_link.symlink_to(test_env / "missing-app-target")

    monkeypatch.delenv("TOPO_SYMLINK_SCAN_USER_DIRS", raising=False)
    with patch("pathlib.Path.home", return_value=test_env):
        assert run_broken_symlink_cleanup(dry_run=True) is None

    monkeypatch.setenv("TOPO_SYMLINK_SCAN_USER_DIRS", "1")
    with patch("pathlib.Path.home", return_value=test_env):
        result = run_broken_symlink_cleanup(dry_run=True)

    assert result == "Found 1 broken user symlinks"
    assert broken_link.is_symlink()


def test_run_sysctl_optimize():
    with (
        patch("src.clean.optimize.shutil.which", return_value="/usr/sbin/sysctl"),
        patch("src.clean.optimize.has_sudo", return_value=True),
        patch("src.clean.optimize.run_command") as mock_run,
    ):
        mock_run.return_value = CommandResult(["sysctl"], 0)
        res = run_sysctl_optimize(dry_run=False)
        assert res == "Kernel memory & cache parameters tuned (sysctl)"


def test_run_tmpfiles_cleanup():
    with (
        patch("src.clean.optimize.shutil.which", return_value="/usr/bin/systemd-tmpfiles"),
        patch("src.clean.optimize.run_command") as mock_run,
    ):
        mock_run.return_value = CommandResult(["systemd-tmpfiles"], 0)
        res = run_tmpfiles_cleanup(dry_run=False)
        assert res == "Systemd tmpfiles clean rules processed"


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

    # is_sqlite_busy() opens its own connection through the same sqlite3 module,
    # so it must be stubbed out or the patched connect would be consumed twice.
    with (
        patch("src.clean.optimize.is_sqlite_busy", return_value=False),
        patch("src.clean.optimize.sqlite3.connect", return_value=fake_conn),
    ):
        result = vacuum_single_db(db)

    assert result == 0
    fake_conn.close.assert_called_once()
