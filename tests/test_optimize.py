import inspect
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src import optimize
from src.core.file_ops import TRASH_UNAVAILABLE_REASON
from src.core.system import CommandResult
from src.optimize import (
    OptimizationRegistry,
    _extract_service_exec_targets,
    _is_any_process_running,
    _is_sqlite_database,
    _points_at_transient_mount,
    _service_exec_target_exists,
    opt_log,
    optimize_system,
    run_autostart_cleanup,
    run_broken_symlink_cleanup,
    run_coredump_cleanup,
    run_desktop_database_refresh,
    run_fccache,
    run_flatpak_repair,
    run_fstrim,
    run_journal_optimization,
    run_ldconfig,
    run_locale_gen,
    run_man_db_refresh,
    run_mime_database_refresh,
    run_package_repo_refresh,
    run_swap_management,
    run_sysctl_optimize,
    run_systemd_user_service_cleanup,
    run_tmpfiles_cleanup,
    run_tracker_miner_reset,
    run_user_systemd_reset_failed,
    run_vacuum_all,
    vacuum_single_db,
)


def test_opt_log_uses_failure_icon_when_unsuccessful(capsys):
    opt_log("task failed", success=False)

    output = capsys.readouterr().out
    assert "✗ task failed" in output
    assert "✓ task failed" not in output


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


def test_optimize_system_caps_worker_pool_and_reports_task_failures(capsys):
    def failing_task(dry_run=False):
        raise RuntimeError("failed")

    tasks = [failing_task] * 6
    with (
        patch.object(OptimizationRegistry, "tasks", tasks),
        patch("src.core.system.authenticate_sudo_session", return_value=True),
        patch(
            "src.optimize.ThreadPoolExecutor",
            wraps=__import__(
                "concurrent.futures", fromlist=["ThreadPoolExecutor"]
            ).ThreadPoolExecutor,
        ) as executor,
    ):
        optimize_system(dry_run=True)

    executor.assert_called_once_with(max_workers=4)
    output = capsys.readouterr().out
    assert "failing_task failed (RuntimeError)" in output


def test_run_systemd_user_service_cleanup_removes_broken_unit(test_env):
    service_dir = test_env / ".config/systemd/user"
    service_dir.mkdir(parents=True)
    service_file = service_dir / "dead-app.service"
    service_file.write_text("[Service]\nExecStart=/missing/dead-app\n")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch("src.optimize.run_command") as mock_run,
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
        patch("src.optimize.COREDUMP_DIR", coredump_dir),
        patch("src.optimize.run_command") as mock_run,
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
        patch("src.optimize.COREDUMP_DIR", coredump_dir),
        patch("src.optimize.run_command") as mock_run,
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
        patch("src.optimize.COREDUMP_DIR", coredump_dir),
        patch(
            "src.optimize.run_command",
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
        patch("src.optimize.COREDUMP_DIR", coredump_dir),
        patch(
            "src.optimize.run_command",
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
        patch("src.optimize.shutil.which", return_value="/usr/sbin/sysctl"),
        patch("src.optimize.has_sudo", return_value=True),
        patch("src.optimize.run_command") as mock_run,
    ):
        mock_run.return_value = CommandResult(["sysctl"], 0)
        res = run_sysctl_optimize(dry_run=False)
        assert res == "Kernel memory & cache parameters tuned (sysctl)"


def test_run_tmpfiles_cleanup():
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/systemd-tmpfiles"),
        patch("src.optimize.run_command") as mock_run,
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
        patch("src.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "src.optimize.run_command",
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
        patch("src.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "src.optimize.run_command",
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
        patch("src.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "src.optimize.run_command",
            return_value=CommandResult(["systemctl"], 0, stdout=""),
        ) as mock_run,
    ):
        result = run_user_systemd_reset_failed(dry_run=False)

    assert result is None
    assert mock_run.call_count == 1


def test_run_vacuum_all_skips_when_browser_is_running(test_env):
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize._is_any_process_running", return_value=True),
    ):
        result = run_vacuum_all(dry_run=False)

    assert result == "Brave, Chrome, Edge, Firefox running; database optimization skipped"


def test_run_desktop_database_refresh_dry_run(test_env):
    app_dir = test_env / ".local/share/applications"
    app_dir.mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize.shutil.which", return_value="/usr/bin/update-desktop-database"),
    ):
        result = run_desktop_database_refresh(dry_run=True)

    assert result == "Desktop application database would be refreshed"


def test_run_mime_database_refresh_dry_run(test_env):
    mime_dir = test_env / ".local/share/mime"
    mime_dir.mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize.shutil.which", return_value="/usr/bin/update-mime-database"),
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
        patch("src.optimize.is_sqlite_busy", return_value=False),
        patch("src.optimize.sqlite3.connect", return_value=fake_conn),
    ):
        result = vacuum_single_db(db)

    assert result == 0
    fake_conn.close.assert_called_once()


def test_opt_log_skipped_and_process_helpers(capsys):
    opt_log("preview", skipped=True)
    assert "preview · skipped" in capsys.readouterr().out
    with patch("src.optimize.shutil.which", return_value=None):
        assert _is_any_process_running(["firefox"]) is False
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/pgrep"),
        patch(
            "src.optimize.run_command",
            side_effect=[CommandResult(["pgrep"], 1), CommandResult(["pgrep"], 0)],
        ),
    ):
        assert _is_any_process_running(["firefox", "chrome"]) is True


def test_sqlite_detection_and_vacuum_skip_guards(tmp_path):
    good = tmp_path / "good.db"
    good.write_bytes(b"SQLite format 3\x00" + b"\0" * 20)
    bad = tmp_path / "bad.db"
    bad.write_text("not sqlite")
    assert _is_sqlite_database(good) is True
    assert _is_sqlite_database(bad) is False
    with patch("src.optimize._is_sqlite_database", return_value=False):
        assert vacuum_single_db(bad) == 0
    with patch("src.optimize._is_sqlite_database", return_value=True):
        assert vacuum_single_db(tmp_path / "x-wal") == 0
    with (
        patch("src.optimize._is_sqlite_database", return_value=True),
        patch("src.optimize.is_file_locked", return_value=True),
    ):
        assert vacuum_single_db(good) == 0
    with (
        patch("src.optimize._is_sqlite_database", return_value=True),
        patch("src.optimize.is_file_locked", return_value=False),
        patch("src.optimize.is_sqlite_busy", return_value=True),
    ):
        assert vacuum_single_db(good) == 0


@pytest.mark.parametrize(
    ("func", "tool", "dry_text", "command"),
    [
        (run_fstrim, "fstrim", "SSD partitions would be trimmed", ["fstrim", "-av"]),
        (run_fccache, "fc-cache", "System font cache would be refreshed", ["fc-cache"]),
        (run_ldconfig, "ldconfig", "Dynamic linker cache would be updated", ["ldconfig"]),
        (
            run_locale_gen,
            "locale-gen",
            "System locale archive would be regenerated",
            ["locale-gen"],
        ),
        (
            run_man_db_refresh,
            "mandb",
            "Manual page database index would be updated",
            ["mandb", "-q"],
        ),
    ],
)
def test_simple_optimization_tasks_support_missing_dry_run_success_and_failure(
    func, tool, dry_text, command
):
    with patch("src.optimize.shutil.which", return_value=None):
        assert func() is None
    with patch("src.optimize.shutil.which", return_value=f"/usr/bin/{tool}"):
        assert dry_text in func(dry_run=True)
    with (
        patch("src.optimize.shutil.which", return_value=f"/usr/bin/{tool}"),
        patch("src.optimize.run_command", return_value=CommandResult(command, 0)) as run,
    ):
        assert func() is not None
        assert run.call_args.args[0] == command
    with (
        patch("src.optimize.shutil.which", return_value=f"/usr/bin/{tool}"),
        patch("src.optimize.run_command", return_value=CommandResult(command, 1)),
    ):
        assert func() is None


def test_sysctl_tmpfiles_and_flatpak_branches():
    with patch("src.optimize.shutil.which", return_value=None):
        assert run_sysctl_optimize() is None
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/sysctl"),
        patch("src.optimize.has_sudo", return_value=False),
    ):
        assert run_sysctl_optimize() is None
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/systemd-tmpfiles"),
        patch("src.optimize.run_command", return_value=CommandResult(["tmpfiles"], 1)),
    ):
        assert run_tmpfiles_cleanup() is None
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/flatpak"),
        patch("src.optimize.has_sudo", return_value=True),
        patch("src.optimize.run_command", return_value=CommandResult(["flatpak"], 0)) as run,
    ):
        assert "verified" in run_flatpak_repair()
        assert run.call_count == 2
    with patch("src.optimize.shutil.which", return_value="/usr/bin/flatpak"):
        assert "would be verified" in run_flatpak_repair(dry_run=True)


def test_autostart_cleanup_no_dir_and_trash_failure_reason(test_env):
    with patch("pathlib.Path.home", return_value=test_env):
        assert run_autostart_cleanup() is None
    d = test_env / ".config/autostart"
    d.mkdir(parents=True)
    f = d / "dead.desktop"
    f.write_text("Exec=/missing/app\n")
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize.safe_remove", return_value=(False, TRASH_UNAVAILABLE_REASON)),
    ):
        assert (
            run_autostart_cleanup()
            == "Kept 1 zombie autostart entries (no trash backend available)"
        )


def test_systemd_service_and_failed_reset_error_paths(test_env):
    with patch("pathlib.Path.home", return_value=test_env):
        assert run_systemd_user_service_cleanup() is None
    d = test_env / ".config/systemd/user"
    d.mkdir(parents=True)
    (d / "bad.service").write_text("ExecStart=/missing/app\n")
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize.safe_remove", return_value=(False, "error")),
    ):
        assert run_systemd_user_service_cleanup() is None
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch("src.optimize.run_command", return_value=CommandResult(["systemctl"], 1)),
    ):
        assert run_user_systemd_reset_failed() is None


def test_service_helpers_and_database_refresh(tmp_path):
    service = tmp_path / "x.service"
    service.write_text('ExecStart=-/missing/app --x\nExecStart="unterminated\nExecStart=\n')
    targets = _extract_service_exec_targets(service)
    assert "/missing/app" in targets
    with patch("src.optimize.shutil.which", return_value=None):
        assert _service_exec_target_exists("missing") is False
    with patch("src.optimize.shutil.which", return_value="/usr/bin/app"):
        assert _service_exec_target_exists("app") is True
    target = tmp_path / "apps"
    target.mkdir()
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/update"),
        patch("src.optimize.run_command", return_value=CommandResult(["update"], 1)),
    ):
        assert optimize.run_desktop_database_refresh() is None


def test_swap_journal_tracker_repo_and_coredump_error_paths(tmp_path):
    with patch("src.optimize.shutil.which", return_value=None):
        assert run_swap_management() is None
        assert run_journal_optimization() is None
        assert run_tracker_miner_reset() is None
        assert run_package_repo_refresh() is None
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/journalctl"),
        patch("src.optimize.run_command", return_value=CommandResult(["journalctl"], 0, stdout="")),
    ):
        assert run_journal_optimization() == "Journal already optimized (under 3 days)"
    with (
        patch(
            "src.optimize.shutil.which",
            side_effect=lambda n: "/usr/bin/tracker" if n == "tracker" else None,
        ),
        patch("src.optimize.run_command", return_value=CommandResult(["tracker"], 1)),
    ):
        assert run_tracker_miner_reset() is None
    with (
        patch(
            "src.optimize.shutil.which",
            side_effect=lambda n: "/usr/bin/pkcon" if n == "pkcon" else None,
        ),
        patch("src.optimize.run_command", return_value=CommandResult(["pkcon"], 0)),
    ):
        assert run_package_repo_refresh() == "Software repository index refreshed"
    with patch("src.optimize.COREDUMP_DIR", tmp_path / "missing"):
        assert run_coredump_cleanup() is None
