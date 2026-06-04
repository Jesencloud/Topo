import os
import time
from pathlib import Path
from unittest.mock import patch

from src.clean.user import clean_system_temp, clean_trash


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
