import inspect
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, mock_open, patch

import pytest

from src import optimize
from src.core.file_ops import TRASH_UNAVAILABLE_REASON
from src.core.system import CommandResult
from src.optimize import (
    _REPO_REFRESH_COMMANDS,
    OptimizationRegistry,
    _extract_service_exec_targets,
    _is_any_process_running,
    _is_sqlite_database,
    _points_at_transient_mount,
    _service_exec_target_exists,
    _swap_is_zram_backed,
    _systemd_timer_enabled,
    _updatedb_is_scheduled,
    _which_admin_tool,
    opt_log,
    optimize_system,
    run_autostart_cleanup,
    run_broken_symlink_cleanup,
    run_coredump_cleanup,
    run_desktop_database_refresh,
    run_fccache,
    run_flatpak_repair,
    run_fstrim,
    run_glib_schema_compile,
    run_icon_cache_refresh,
    run_journal_optimization,
    run_ldconfig,
    run_locale_gen,
    run_locate_db_refresh,
    run_man_db_refresh,
    run_mime_database_refresh,
    run_package_repo_refresh,
    run_swap_management,
    run_system_systemd_reset_failed,
    run_systemd_user_service_cleanup,
    run_tmpfiles_cleanup,
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


def test_run_system_systemd_reset_failed_skips_without_sudo():
    with (
        patch("src.optimize.has_sudo", return_value=False),
        patch("src.optimize.run_command") as mock_run,
    ):
        assert run_system_systemd_reset_failed(dry_run=False) is None

    mock_run.assert_not_called()


def test_run_system_systemd_reset_failed_uses_sudo_and_the_system_scope():
    list_result = CommandResult(
        ["systemctl"],
        0,
        stdout="nfs-mount.service loaded failed failed NFS mount\n",
    )

    with (
        patch("src.optimize.has_sudo", return_value=True),
        patch("src.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "src.optimize.run_command",
            side_effect=[list_result, CommandResult(["systemctl"], 0)],
        ) as mock_run,
    ):
        result = run_system_systemd_reset_failed(dry_run=False)

    assert result == "Reset 1 failed system systemd unit state(s)"
    # No --user anywhere: the system manager is the default scope, and the reset
    # is the only half that needs privileges -- listing must not ask for them.
    assert mock_run.call_args_list[0].args[0] == [
        "systemctl",
        "list-units",
        "--state=failed",
        "--no-legend",
        "--no-pager",
        "--plain",
    ]
    assert mock_run.call_args_list[0].kwargs.get("use_sudo") is not True
    assert mock_run.call_args_list[1].args[0] == ["systemctl", "reset-failed"]
    assert mock_run.call_args_list[1].kwargs["use_sudo"] is True


def test_run_vacuum_all_skips_when_browser_is_running(test_env):
    """The label list doubles as the record of which browsers are covered.

    Both families are now taken from core.browser_paths rather than a private
    table, so every browser protection and cache cleanup already know about is
    vacuumed too -- a Zen Browser places.sqlite bloats exactly like Firefox's.
    """
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize._is_any_process_running", return_value=True),
    ):
        result = run_vacuum_all(dry_run=False)

    assert (
        result == "Brave Browser, Chromium, Firefox, Floorp, Google Chrome, LibreWolf, "
        "Microsoft Edge, Opera, Thorium, Vivaldi, Waterfox, Yandex Browser, Zen Browser "
        "running; database optimization skipped"
    )


def _write_sqlite_db(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"SQLite format 3\x00" + b"\x00" * 64)
    return path


def test_vacuum_finds_databases_behind_a_wildcard_profile_directory(test_env):
    """A wildcard mid-path used to match nothing, so Firefox was never vacuumed.

    The old resolver took Path(pattern).parent and required it to exist, which
    for "~/.mozilla/firefox/*/places.sqlite" asked about a directory literally
    named "*". It never existed, so both Firefox entries were dead weight.
    """
    profile = test_env / ".mozilla/firefox/abc123.default-release"
    _write_sqlite_db(profile / "places.sqlite")
    _write_sqlite_db(profile / "favicons.sqlite")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize._is_any_process_running", return_value=False),
    ):
        assert run_vacuum_all(dry_run=True) == "Found 2 database(s) to optimize"


def test_vacuum_covers_flatpak_and_non_default_chromium_profiles(test_env):
    """Every profile dir counts, not just Default, and Flatpak layouts too."""
    _write_sqlite_db(test_env / ".config/google-chrome/Default/History")
    _write_sqlite_db(test_env / ".config/google-chrome/Profile 1/History")
    _write_sqlite_db(test_env / ".config/google-chrome/Default/Network/Cookies")
    _write_sqlite_db(
        test_env / ".var/app/org.mozilla.firefox/.mozilla/firefox/x.default/places.sqlite"
    )

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize._is_any_process_running", return_value=False),
    ):
        assert run_vacuum_all(dry_run=True) == "Found 4 database(s) to optimize"


def test_every_browser_pattern_is_relative_and_wildcards_one_profile_level():
    """home.glob() rejects an absolute pattern, and "~" is not expanded by it.

    At most one "*" per pattern: the profile globs from core.browser_paths
    already end at a single profile directory, so a root that carried its own
    trailing "*" would produce ".mozilla/firefox/*/*/places.sqlite" -- one level
    too deep, matching nothing and failing silently, which is the same class of
    bug this table once fixed. Zero is legitimate for the one browser whose root
    is its profile.
    """
    for _label, processes, patterns in optimize._BROWSER_DB_TARGETS:
        assert processes, _label
        for pattern in patterns:
            assert not pattern.startswith(("/", "~")), pattern
            assert pattern.count("*") <= 1, pattern


def test_vacuum_reaches_snap_and_non_firefox_gecko_profiles(test_env):
    """The paths come from core.browser_paths, so both quirks are covered at once.

    The chromium snap's user-data-dir is ~/snap/chromium/common/chromium, and
    every Gecko browser except Firefox keeps its profiles directly under the
    root instead of one level down under "firefox/".
    """
    _write_sqlite_db(test_env / "snap/chromium/common/chromium/Default/History")
    _write_sqlite_db(test_env / ".zen/abc.default/places.sqlite")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize._is_any_process_running", return_value=False),
    ):
        assert run_vacuum_all(dry_run=True) == "Found 2 database(s) to optimize"


def test_vacuum_reaches_a_browser_whose_root_is_its_profile(test_env):
    """Opera's profile folder is ~/.config/opera itself, with no Default/ below.

    Assuming the usual Chromium container layout for every Chromium build looks
    one directory too deep and finds nothing, which is invisible in a size total.
    """
    _write_sqlite_db(test_env / ".config/opera/History")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize._is_any_process_running", return_value=False),
    ):
        assert run_vacuum_all(dry_run=True) == "Found 1 database(s) to optimize"


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


def test_is_any_process_running_trims_to_the_kernel_comm_limit():
    """Ubuntu's "chromium-browser" is 16 characters, one over what comm holds.

    pgrep rejects a pattern that long instead of matching the truncated name, and
    the non-zero exit read as "not running" -- which let a live Chromium's
    databases be vacuumed underneath it.
    """
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/pgrep"),
        patch("src.optimize.run_command", return_value=CommandResult(["pgrep"], 1)) as run,
    ):
        assert _is_any_process_running(["chromium-browser"]) is False

    assert run.call_args.args[0] == ["pgrep", "-x", "chromium-browse"]


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
    # Both lookups: the sbin-aware one would otherwise find the real
    # /usr/sbin/fstrim on the host and the "not installed" branch would never run.
    with (
        patch("src.optimize.shutil.which", return_value=None),
        patch("src.optimize._which_admin_tool", return_value=None),
    ):
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


def test_tmpfiles_and_flatpak_branches():
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


def test_swap_journal_repo_and_coredump_error_paths(tmp_path):
    with (
        patch("src.optimize.shutil.which", return_value=None),
        patch("src.optimize._which_admin_tool", return_value=None),
    ):
        assert run_swap_management() is None
        assert run_journal_optimization() is None
        assert run_package_repo_refresh() is None
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/journalctl"),
        patch("src.optimize.run_command", return_value=CommandResult(["journalctl"], 0, stdout="")),
    ):
        assert run_journal_optimization() == "Journal already optimized (under 3 days)"
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


# Enough free RAM to clear the _MIN_RAM_SWAP_RATIO gate, so anything that stops
# the reset in these tests is the zram guard and not the RAM arithmetic.
_MEMINFO_RAM_RICH = "MemAvailable: 16000000 kB\nSwapTotal: 8000000 kB\nSwapFree: 2000000 kB\n"
_SWAPS_HEADER = "Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority\n"


def _swaps_table(tmp_path, device):
    table = tmp_path / "swaps"
    table.write_text(f"{_SWAPS_HEADER}{device}\tpartition\t8388604\t\t524288\t\t100\n")
    return table


def test_zram_backed_swap_is_never_reset(tmp_path):
    """swapoff -a would strand a zram box with no swap until it reboots.

    swapon -a only re-enables /etc/fstab entries, and zram swap has none -- it is
    brought up by systemd-zram-setup@zramN.service. The dry run must stay quiet
    too, or it promises a reset that will not happen.
    """
    run_command = MagicMock()
    with (
        patch("src.optimize.shutil.which", return_value="/usr/sbin/swapoff"),
        patch("src.optimize._SWAPS_TABLE", _swaps_table(tmp_path, "/dev/zram0")),
        patch("builtins.open", mock_open(read_data=_MEMINFO_RAM_RICH)),
        patch("src.optimize.run_command", run_command),
    ):
        assert run_swap_management() is None
        assert run_swap_management(dry_run=True) is None
    run_command.assert_not_called()


def test_fstab_backed_swap_is_still_reset(tmp_path):
    """The guard must not disable the task on machines swapon -a can restore."""
    with (
        patch("src.optimize.shutil.which", return_value="/usr/sbin/swapoff"),
        patch("src.optimize._SWAPS_TABLE", _swaps_table(tmp_path, "/dev/sda2")),
        patch("builtins.open", mock_open(read_data=_MEMINFO_RAM_RICH)),
        patch("src.optimize.run_command", return_value=CommandResult(["swapoff"], 0)),
    ):
        assert run_swap_management(dry_run=True).startswith("Swap would be reset")
        assert run_swap_management().startswith("Swap reset successful")


def test_unreadable_swap_table_counts_as_unsafe(tmp_path):
    """No proof the reset is reversible is not the same as proof that it is."""
    with patch("src.optimize._SWAPS_TABLE", tmp_path / "missing"):
        assert _swap_is_zram_backed() is True


def test_glib_schema_compile_follows_the_shared_refresh_helper(test_env):
    schemas = test_env / ".local/share/glib-2.0/schemas"
    with patch("pathlib.Path.home", return_value=test_env):
        assert run_glib_schema_compile() is None  # directory absent
    schemas.mkdir(parents=True)
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize.shutil.which", return_value="/usr/bin/glib-compile-schemas"),
        patch("src.optimize.run_command", return_value=CommandResult(["glib"], 0)) as run,
    ):
        assert (
            run_glib_schema_compile(dry_run=True)
            == "User GSettings schema cache would be refreshed"
        )
        assert run_glib_schema_compile() == "User GSettings schema cache refreshed"
        assert run.call_args.args[0] == ["glib-compile-schemas", str(schemas)]


def test_icon_cache_refresh_targets_theme_directories_not_the_root(test_env):
    """gtk-update-icon-cache exits non-zero on a directory without index.theme.

    So the themes are enumerated by that file: a bare ~/.local/share/icons, or a
    subdirectory holding loose icons, must not be handed to the tool.
    """
    icons = test_env / ".local/share/icons"
    theme = icons / "MyTheme"
    theme.mkdir(parents=True)
    (theme / "index.theme").write_text("[Icon Theme]\nName=MyTheme\n")
    (icons / "loose").mkdir()

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize.shutil.which", return_value="/usr/bin/gtk-update-icon-cache"),
        patch("src.optimize.run_command", return_value=CommandResult(["gtk"], 0)) as run,
    ):
        assert run_icon_cache_refresh(dry_run=True) == "1 user icon theme cache(s) would be rebuilt"
        assert run_icon_cache_refresh() == "Rebuilt 1 user icon theme cache(s)"
        # -q and -f only: GTK3 reads -t as --ignore-theme-index, GTK4 as --index-only.
        assert run.call_args.args[0] == ["gtk-update-icon-cache", "-q", "-f", str(theme)]


def test_icon_cache_refresh_error_paths(test_env):
    with patch("src.optimize.shutil.which", return_value=None):
        assert run_icon_cache_refresh() is None
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize.shutil.which", return_value="/usr/bin/gtk-update-icon-cache"),
    ):
        assert run_icon_cache_refresh() is None  # no themes installed
    theme = test_env / ".local/share/icons/Broken"
    theme.mkdir(parents=True)
    (theme / "index.theme").write_text("[Icon Theme]\n")
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize.shutil.which", return_value="/usr/bin/gtk-update-icon-cache"),
        patch("src.optimize.run_command", return_value=CommandResult(["gtk"], 1)),
    ):
        assert run_icon_cache_refresh() is None


def test_font_cache_refresh_covers_the_user_cache_before_the_system_one():
    """The user cache is the one that goes stale, so it must not need sudo.

    Plain ``fc-cache`` writes ~/.cache/fontconfig only; /var/cache/fontconfig is a
    second, privileged pass. A machine without sudo still gets the first.
    """
    with patch("src.optimize.shutil.which", return_value=None):
        assert run_fccache() is None
    with patch("src.optimize.shutil.which", return_value="/usr/bin/fc-cache"):
        assert run_fccache(dry_run=True) == "Font caches would be refreshed (fc-cache)"
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/fc-cache"),
        patch("src.optimize.has_sudo", return_value=False),
        patch("src.optimize.run_command", return_value=CommandResult(["fc-cache"], 0)) as run,
    ):
        assert run_fccache() == "User font cache refreshed (fc-cache)"
        assert run.call_count == 1
        assert run.call_args.kwargs.get("use_sudo") is not True
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/fc-cache"),
        patch("src.optimize.has_sudo", return_value=True),
        patch("src.optimize.run_command", return_value=CommandResult(["fc-cache"], 0)) as run,
    ):
        assert run_fccache() == "User & system font caches refreshed (fc-cache)"
        assert run.call_args_list[1].kwargs["use_sudo"] is True
    # A failed system pass still leaves the user cache rebuilt, so the task
    # reports what it actually did rather than nothing.
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/fc-cache"),
        patch("src.optimize.has_sudo", return_value=True),
        patch(
            "src.optimize.run_command",
            side_effect=[CommandResult(["fc-cache"], 0), CommandResult(["fc-cache"], 1)],
        ),
    ):
        assert run_fccache() == "User font cache refreshed (fc-cache)"
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/fc-cache"),
        patch("src.optimize.run_command", return_value=CommandResult(["fc-cache"], 1)),
    ):
        assert run_fccache() is None


def test_systemd_timer_enabled_reads_the_verdict_lines_not_the_exit_code():
    """is-enabled exits non-zero as soon as one listed unit is not enabled.

    The list deliberately names units that only some distros ship, so a non-zero
    status is the normal case and the answer has to come from stdout.
    """
    with patch("src.optimize.shutil.which", return_value=None):
        assert _systemd_timer_enabled(("a.timer",)) is False
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "src.optimize.run_command",
            return_value=CommandResult(["systemctl"], 1, stdout="not-found\nenabled\n"),
        ),
    ):
        assert _systemd_timer_enabled(("mlocate-updatedb.timer", "plocate-updatedb.timer")) is True
    with (
        patch("src.optimize.shutil.which", return_value="/usr/bin/systemctl"),
        patch(
            "src.optimize.run_command",
            return_value=CommandResult(["systemctl"], 1, stdout="not-found\nstatic\n"),
        ),
    ):
        assert _systemd_timer_enabled(("a.timer", "b.timer")) is False


def test_locate_db_refresh_defers_to_an_enabled_distro_timer():
    """updatedb walks every filesystem; doing that twice a day buys nothing."""
    with (
        patch("src.optimize.shutil.which", return_value=None),
        patch("src.optimize._which_admin_tool", return_value=None),
    ):
        assert run_locate_db_refresh() is None
    with (
        patch("src.optimize._which_admin_tool", return_value="/usr/sbin/updatedb"),
        patch("src.optimize.has_sudo", return_value=False),
        patch("src.optimize.run_command") as run,
    ):
        assert run_locate_db_refresh() is None
        run.assert_not_called()
    with (
        patch("src.optimize._which_admin_tool", return_value="/usr/sbin/updatedb"),
        patch("src.optimize.has_sudo", return_value=True),
        patch("src.optimize._updatedb_is_scheduled", return_value=True),
        patch("src.optimize.run_command") as run,
    ):
        assert run_locate_db_refresh() is None
        assert run_locate_db_refresh(dry_run=True) is None
        run.assert_not_called()


def test_updatedb_schedule_check_covers_cron_as_well_as_timers(tmp_path):
    """Debian schedules the same rebuild with cron, sometimes only with cron.

    plocate on Debian 13 ships /etc/cron.daily/plocate beside its timer, and
    mlocate on older releases ships nothing but the cron entry -- a systemd-only
    check reads both as "nobody maintains this index".
    """
    cron_job = tmp_path / "plocate"
    with (
        patch("src.optimize._systemd_timer_enabled", return_value=False),
        patch("src.optimize._UPDATEDB_CRON_JOBS", (cron_job,)),
    ):
        assert _updatedb_is_scheduled() is False
        cron_job.write_text("#!/bin/sh\n")
        # run-parts skips a non-executable entry, so a disabled job is not a job.
        cron_job.chmod(0o644)
        assert _updatedb_is_scheduled() is False
        cron_job.chmod(0o755)
        assert _updatedb_is_scheduled() is True
    # An enabled timer short-circuits before any file is touched.
    with (
        patch("src.optimize._systemd_timer_enabled", return_value=True),
        patch("src.optimize._UPDATEDB_CRON_JOBS", ()),
    ):
        assert _updatedb_is_scheduled() is True


def test_locate_db_refresh_runs_when_nothing_else_maintains_the_index():
    with (
        patch("src.optimize._which_admin_tool", return_value="/usr/sbin/updatedb"),
        patch("src.optimize.has_sudo", return_value=True),
        patch("src.optimize._updatedb_is_scheduled", return_value=False),
        patch("src.optimize.run_command", return_value=CommandResult(["updatedb"], 0)) as run,
    ):
        assert run_locate_db_refresh(dry_run=True) == "locate database would be rebuilt (updatedb)"
        assert run_locate_db_refresh() == "locate database rebuilt (updatedb)"
        assert run.call_args.args[0] == ["updatedb"]
        assert run.call_args.kwargs["use_sudo"] is True
        # A full filesystem walk must not be cut off by the default ceiling.
        assert run.call_args.kwargs["timeout"] == optimize.UPDATEDB_TIMEOUT
    with (
        patch("src.optimize._which_admin_tool", return_value="/usr/sbin/updatedb"),
        patch("src.optimize.has_sudo", return_value=True),
        patch("src.optimize._updatedb_is_scheduled", return_value=False),
        patch("src.optimize.run_command", return_value=CommandResult(["updatedb"], 1)),
    ):
        assert run_locate_db_refresh() is None


def test_repo_refresh_prefers_the_native_package_manager_over_packagekit():
    """pkcon only proxies the backend below it, and hides its errors one layer up."""
    order = [tool for tool, _ in _REPO_REFRESH_COMMANDS]
    assert order.index("dnf5") < order.index("dnf") < order.index("pkcon")
    assert order.index("zypper") < order.index("pkcon")
    assert order.index("pacman") < order.index("pkcon")
    assert order.index("apt-get") < order.index("pkcon")
    assert order.index("pkcon") < order.index("apt-file")
    # -Sy without a matching upgrade is what leaves an Arch box half-upgraded;
    # only the files database may be synced on its own.
    pacman = next(cmd for tool, cmd in _REPO_REFRESH_COMMANDS if tool == "pacman")
    assert "-Sy" not in pacman
    assert "-Fy" in pacman
    # apt, not apt-get, is the one that warns its CLI is unfit for scripts.
    apt = next(cmd for tool, cmd in _REPO_REFRESH_COMMANDS if tool == "apt-get")
    assert apt[:2] == ["apt-get", "update"]

    with (
        patch(
            "src.optimize.shutil.which",
            side_effect=lambda n: f"/usr/bin/{n}" if n in {"dnf", "pkcon"} else None,
        ),
        patch("src.optimize.run_command", return_value=CommandResult(["dnf"], 0)) as run,
    ):
        assert (
            run_package_repo_refresh(dry_run=True) == "Software repository index would be refreshed"
        )
        assert run_package_repo_refresh() == "Software repository index refreshed"
        assert run.call_args.args[0] == ["dnf", "makecache"]
        assert run.call_args.kwargs["timeout"] == optimize.REPO_REFRESH_TIMEOUT
    with (
        patch(
            "src.optimize.shutil.which",
            side_effect=lambda n: "/usr/bin/dnf5" if n == "dnf5" else None,
        ),
        patch("src.optimize.run_command", return_value=CommandResult(["dnf5"], 1)),
    ):
        assert run_package_repo_refresh() is None


def test_repo_refresh_reaches_apt_on_debian_and_ubuntu():
    """Without apt in the table, a deb machine falls through to nothing.

    pkcon needs PackageKit installed and apt-file is rarely present, so the task
    was a no-op on the whole Debian family while every other package manager got
    a native command.
    """
    with (
        patch(
            "src.optimize.shutil.which",
            side_effect=lambda n: f"/usr/bin/{n}" if n in {"apt-get", "apt-file"} else None,
        ),
        patch("src.optimize.run_command", return_value=CommandResult(["apt-get"], 0)) as run,
    ):
        assert run_package_repo_refresh() == "Software repository index refreshed"
        assert run.call_args.args[0][:2] == ["apt-get", "update"]
        assert run.call_args.kwargs["use_sudo"] is True


def test_which_admin_tool_falls_back_to_the_sbin_directories(tmp_path):
    """Debian leaves /usr/sbin out of a normal user's PATH, so which() misses.

    The commands themselves are fine -- they run under sudo, whose secure_path
    covers sbin -- but the lookup gating them would report fstrim, swapoff,
    ldconfig, locale-gen and updatedb as not installed and skip all five.
    """
    with patch("src.optimize.shutil.which", return_value="/usr/bin/fstrim"):
        assert _which_admin_tool("fstrim") == "/usr/bin/fstrim"

    sbin = tmp_path / "sbin"
    sbin.mkdir()
    tool = sbin / "fstrim"
    with (
        patch("src.optimize.shutil.which", return_value=None),
        patch("src.optimize._SBIN_DIRS", (str(sbin),)),
    ):
        assert _which_admin_tool("fstrim") is None  # nothing there yet
        tool.write_text("#!/bin/sh\n")
        tool.chmod(0o644)
        assert _which_admin_tool("fstrim") is None  # present but not executable
        tool.chmod(0o755)
        assert _which_admin_tool("fstrim") == str(tool)


def test_chromium_snap_profiles_are_covered(test_env):
    """Ubuntu ships chromium only as a snap, so this is its default layout."""
    db = _write_sqlite_db(test_env / "snap/chromium/common/chromium/Default/History")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.optimize._is_any_process_running", return_value=False),
    ):
        assert run_vacuum_all(dry_run=True) == "Found 1 database(s) to optimize"
    assert db.exists()
