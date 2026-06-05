from unittest.mock import patch

import pytest

from src.clean import optimize, runner


class FakeStdin:
    def __init__(self, keys):
        self.keys = list(keys)

    def isatty(self):
        return True

    def fileno(self):
        return 0

    def read(self, size):
        assert size == 1
        return self.keys.pop(0)


@pytest.mark.parametrize("module", [runner, optimize])
def test_sudo_choice_ignores_unrecognized_keys(module):
    stdin = FakeStdin(["x", "1", "\r"])

    with (
        patch.object(module.sys, "stdin", stdin),
        patch.object(module.termios, "tcgetattr", return_value=[]),
        patch.object(module.termios, "tcsetattr"),
        patch.object(module.tty, "setraw"),
    ):
        assert module._read_sudo_choice() == "\r"


@pytest.mark.parametrize("module", [runner, optimize])
def test_sudo_choice_ignores_escape_sequences(module):
    stdin = FakeStdin(["\x1b", "[", "A", " "])

    with (
        patch.object(module.sys, "stdin", stdin),
        patch.object(module.termios, "tcgetattr", return_value=[]),
        patch.object(module.termios, "tcsetattr"),
        patch.object(module.tty, "setraw"),
        patch.object(
            module.select,
            "select",
            side_effect=[
                ([stdin], [], []),
                ([stdin], [], []),
                ([], [], []),
            ],
        ),
    ):
        assert module._read_sudo_choice() == " "


def test_clean_space_skips_clean_without_sudo():
    def no_op(*args, **kwargs):
        return 0, 0, 0

    with (
        patch("src.clean.runner._read_sudo_choice", return_value=" "),
        patch("src.clean.runner.system.ensure_sudo_session") as mock_sudo,
        patch("src.clean.runner.proactive_app_detection", return_value={}),
        patch("src.clean.runner.record_history_session"),
        patch("src.clean.runner.clean_package_manager", side_effect=no_op) as mock_pkg,
        patch("src.clean.runner.clean_orphaned_packages", side_effect=no_op) as mock_orphans,
        patch("src.clean.runner.clean_journal", side_effect=no_op) as mock_journal,
        patch("src.clean.runner.clean_zombies", side_effect=no_op) as mock_zombies,
        patch("src.clean.runner.clean_user_data", side_effect=no_op) as mock_user,
        patch("src.clean.runner.clean_apps_deep", side_effect=no_op) as mock_apps,
        patch("src.clean.runner.clean_developer_tools", side_effect=no_op) as mock_dev,
        patch("src.clean.runner.ScanCache.clear"),
    ):
        result = runner.run_clean(dry_run=False)

    assert result is False
    mock_sudo.assert_not_called()
    mock_pkg.assert_not_called()
    mock_orphans.assert_not_called()
    mock_journal.assert_not_called()
    mock_zombies.assert_not_called()
    mock_user.assert_not_called()
    mock_apps.assert_not_called()
    mock_dev.assert_not_called()
