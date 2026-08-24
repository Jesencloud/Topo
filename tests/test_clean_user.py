import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.clean.user import (
    clean_backup_files,
    clean_system_temp,
    clean_thumbnails,
    clean_trash,
    clean_user_data,
    clean_user_logs,
)


def test_clean_trash_dry_run(test_env):
    """Verify trash cleanup in dry-run mode (should only report size)."""
    trash_dir = test_env / ".local/share/Trash/files"
    trash_dir.mkdir(parents=True)
    (trash_dir / "junk.txt").write_text("garbage")

    # size of 'garbage' is 7 bytes
    with patch("pathlib.Path.home", return_value=test_env):
        size, items, cats = clean_trash(dry_run=True)

    assert size == 7
    assert items == 1
    assert (trash_dir / "junk.txt").exists()


@patch("shutil.which")
@patch("src.clean.user.run_command")
def test_clean_trash_execution_gio(mock_run, mock_which, test_env):
    """Verify trash cleanup using 'gio' command."""
    mock_which.side_effect = lambda x: "/usr/bin/gio" if x == "gio" else None

    # Create a dummy file to ensure total_cleaned > 0
    trash_dir = test_env / ".local/share/Trash/files"
    trash_dir.mkdir(parents=True, exist_ok=True)
    (trash_dir / "test.txt").write_text("content")

    with patch("pathlib.Path.home", return_value=test_env):
        clean_trash(dry_run=False)

    mock_run.assert_called_with(["gio", "trash", "--empty"], capture=True, timeout=30)


def test_clean_trash_fallback_uses_safe_remove_and_real_uid(test_env):
    """Without gio, the fallback empties the home Trash via safe_remove (not a
    bare rmtree of a literal /tmp/trash-$USER path) and counts only what went."""
    trash = test_env / ".local/share/Trash"
    (trash / "files").mkdir(parents=True)
    (trash / "files" / "junk.bin").write_text("0123456789")  # 10 bytes

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("shutil.which", return_value=None),  # no gio -> fallback
        patch("os.getuid", return_value=999999),  # /tmp/.Trash-999999 won't exist
    ):
        size, items, cats = clean_trash(dry_run=False)

    assert items == 1
    assert size == 10
    assert trash.exists()  # recreated empty
    assert not (trash / "files").exists()


def test_clean_system_temp_only_removes_stale_user_owned_items(test_env):
    fake_tmp = test_env / "tmp"
    fake_var_tmp = test_env / "var_tmp"
    fake_tmp.mkdir()
    fake_var_tmp.mkdir()

    stale = fake_tmp / "stale-build"
    fresh = fake_tmp / "fresh-build"
    systemd = fake_tmp / "systemd-private-test"
    hidden = fake_tmp / ".hidden-temp"
    stale.write_text("old")
    fresh.write_text("new")
    systemd.write_text("skip")
    hidden.write_text("skip")

    old_time = time.time() - 5 * 86400
    os.utime(stale, (old_time, old_time))

    def fake_path(value):
        if value == "/tmp":
            return fake_tmp
        if value == "/var/tmp":
            return fake_var_tmp
        return Path(value)

    with patch("src.clean.user.Path", side_effect=fake_path):
        size, items, categories = clean_system_temp(dry_run=False, min_age_days=3)

    assert size == 3
    assert items == 1
    assert categories == 1
    assert not stale.exists()
    assert fresh.exists()
    assert systemd.exists()
    assert hidden.exists()


def test_user_cleaners_return_zero_for_missing_paths(monkeypatch, test_env):
    monkeypatch.setattr("pathlib.Path.home", lambda: test_env)
    assert clean_user_logs() == (0, 0, 0)
    assert clean_backup_files() == (0, 0, 0)
    assert clean_thumbnails() == (0, 0, 0)


def test_clean_trash_dry_run_empty_and_gio_failure_falls_back(test_env):
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("shutil.which", return_value="/usr/bin/gio"),
        patch("src.clean.user.get_size_fast", return_value=0),
    ):
        assert clean_trash(dry_run=True) == (0, 0, 0)

    trash = test_env / ".local/share/Trash"
    (trash / "files").mkdir(parents=True)
    (trash / "files/item").write_text("abc")
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("shutil.which", return_value="/usr/bin/gio"),
        patch("src.clean.user.run_command", return_value=SimpleNamespace(ok=False)),
        patch("src.clean.user.safe_remove", return_value=(True, 3)) as remove,
    ):
        assert clean_trash() == (3, 1, 1)
        remove.assert_called_once()


def test_clean_trash_fallback_dry_run_counts_home_and_tmp_dirs(test_env):
    trash = test_env / ".local/share/Trash"
    trash.mkdir(parents=True)
    temp_trash = test_env / "tmp-trash"
    temp_trash.mkdir()

    def fake_path(value):
        if str(value).startswith("/tmp/.Trash-"):
            return temp_trash
        return Path(value)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("shutil.which", return_value=None),
        patch("src.clean.user.Path", side_effect=fake_path),
        patch("src.clean.user.get_size_fast", side_effect=[2, 4]),
    ):
        assert clean_trash(dry_run=True) == (6, 2, 1)


def test_clean_system_temp_handles_dirs_recent_foreign_and_stat_errors(test_env):
    fake_tmp = test_env / "tmp"
    fake_var = test_env / "var"
    fake_tmp.mkdir()
    fake_var.mkdir()
    stale_dir = fake_tmp / "stale-dir"
    stale_dir.mkdir()
    (stale_dir / "old").write_text("old")
    foreign = fake_tmp / "foreign"
    foreign.write_text("x")
    recent = fake_tmp / "recent"
    recent.write_text("x")
    old = time.time() - 10 * 86400
    os.utime(stale_dir, (old, old))
    os.utime(foreign, (old, old))
    os.utime(recent, (time.time(), time.time()))

    real_stat = Path.stat

    def stat(path, *args, **kwargs):
        if path == foreign:
            raise OSError("unreadable")
        return real_stat(path, *args, **kwargs)

    def fake_path(value):
        return fake_tmp if value == "/tmp" else fake_var if value == "/var/tmp" else Path(value)

    with (
        patch("src.clean.user.Path", side_effect=fake_path),
        patch("src.clean.user.clean_path_by_age", return_value=(5, 1)),
        patch.object(Path, "stat", stat),
    ):
        assert clean_system_temp(dry_run=True, min_age_days=3) == (5, 1, 1)


def test_clean_user_logs_known_and_nested_files_dry_run_and_actual(test_env):
    known = test_env / ".xsession-errors"
    known.write_bytes(b"known")
    nested = test_env / ".local/share/app/logs"
    nested.mkdir(parents=True)
    old_log = nested / "old.log"
    old_log.write_bytes(b"nested")
    os.utime(old_log, (time.time() - 40 * 86400,) * 2)
    config = test_env / ".config/app/Logs"
    config.mkdir(parents=True)
    (config / "fresh.log").write_text("fresh")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.user.safe_remove", return_value=(True, 5)),
    ):
        size, items, category = clean_user_logs(dry_run=True)
        assert size == 11 and items == 2 and category == 1
        size, items, category = clean_user_logs(dry_run=False)
        assert size == 11 and items == 2 and category == 1


def test_clean_user_logs_skips_zero_and_remove_failure(test_env):
    known = test_env / ".xsession-errors"
    known.write_text("")
    log_dir = test_env / ".local/share/app/logs"
    log_dir.mkdir(parents=True)
    log = log_dir / "old.log"
    log.write_text("old")
    os.utime(log, (time.time() - 40 * 86400,) * 2)
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.user.safe_remove", return_value=(False, 0)),
    ):
        assert clean_user_logs() == (0, 0, 0)


def test_clean_backup_files_dry_run_and_actual(test_env):
    root = test_env / "Documents/sub"
    root.mkdir(parents=True)
    (root / "editor~").write_bytes(b"a")
    (root / "swap.swp").write_bytes(b"bb")
    (root / "normal.txt").write_bytes(b"ccc")
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.user.safe_remove", return_value=(True, 1)),
    ):
        assert clean_backup_files(dry_run=True) == (3, 2, 1)
        assert clean_backup_files() == (3, 2, 1)


def test_clean_thumbnails_empty_failure_and_success(test_env):
    thumb = test_env / ".cache/thumbnails"
    thumb.mkdir(parents=True)
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.user.get_size_fast", return_value=0),
    ):
        assert clean_thumbnails() == (0, 0, 0)
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.user.get_size_fast", return_value=4),
        patch("src.clean.user.safe_remove", return_value=(False, 0)),
    ):
        assert clean_thumbnails() == (0, 0, 0)
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.user.get_size_fast", return_value=4),
        patch("src.clean.user.safe_remove", return_value=(True, 4)),
    ):
        assert clean_thumbnails() == (4, 1, 1)


def test_clean_user_data_aggregates_all_cleaners():
    values = [(1, 2, 3)] * 5
    with patch.multiple(
        "src.clean.user",
        clean_trash=lambda _: values[0],
        clean_system_temp=lambda _: values[0],
        clean_user_logs=lambda _: values[0],
        clean_backup_files=lambda _: values[0],
        clean_thumbnails=lambda _: values[0],
    ):
        assert clean_user_data(True) == (5, 10, 15)
