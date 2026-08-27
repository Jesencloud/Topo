from unittest.mock import patch

import pytest

from src import optimize
from src.clean import runner
from src.core import terminal_state


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


def test_sudo_choice_ignores_unrecognized_keys():
    stdin = FakeStdin(["x", "1", "\r"])

    with (
        patch.object(terminal_state.sys, "stdin", stdin),
        patch.object(terminal_state.termios, "tcgetattr", return_value=[]),
        patch.object(terminal_state.termios, "tcsetattr"),
        patch.object(terminal_state.tty, "setraw"),
    ):
        assert terminal_state.read_sudo_choice() == "\r"


def test_sudo_choice_ctrl_c_cancels_and_restores_terminal():
    stdin = FakeStdin(["\x03"])

    with (
        patch.object(terminal_state.sys, "stdin", stdin),
        patch.object(terminal_state.termios, "tcgetattr", return_value=["old"]),
        patch.object(terminal_state.tty, "setraw"),
        patch.object(terminal_state, "restore_raw_state") as restore_raw_state,
        pytest.raises(KeyboardInterrupt),
    ):
        terminal_state.read_sudo_choice()

    restore_raw_state.assert_called_once_with(0, ["old"])


def test_sudo_confirmation_ctrl_c_cancels_before_password_prompt():
    with (
        patch("src.core.terminal_state.read_sudo_choice", side_effect=KeyboardInterrupt),
        patch("src.core.system.ensure_sudo_session") as ensure_sudo_session,
        patch("builtins.print"),
    ):
        assert (
            runner.system.authenticate_sudo_session(
                False, request_subject="System caches", action="cleanup"
            )
            is False
        )

    assert runner.system.SUDO_CANCELLED is True
    ensure_sudo_session.assert_not_called()


def test_sudo_choice_ignores_escape_sequences():
    stdin = FakeStdin(["\x1b", "[", "A", " "])

    with (
        patch.object(terminal_state.sys, "stdin", stdin),
        patch.object(terminal_state.termios, "tcgetattr", return_value=[]),
        patch.object(terminal_state.termios, "tcsetattr"),
        patch.object(terminal_state.tty, "setraw"),
        patch.object(
            terminal_state.select,
            "select",
            side_effect=[
                ([stdin], [], []),
                ([stdin], [], []),
                ([], [], []),
            ],
        ),
    ):
        assert terminal_state.read_sudo_choice() == " "


def test_clean_space_skips_clean_without_sudo():
    def no_op(*args, **kwargs):
        return 0, 0, 0

    with (
        patch("src.core.terminal_state.read_sudo_choice", return_value=" "),
        patch("src.core.system.ensure_sudo_session") as mock_sudo,
        patch("src.clean.runner.proactive_app_detection", return_value={}),
        patch("src.clean.runner.record_history_session"),
        patch("src.clean.runner.clean_system_data", side_effect=no_op) as mock_system,
        patch("src.clean.runner.clean_user_data", side_effect=no_op) as mock_user,
        patch("src.clean.runner.clean_apps_deep", side_effect=no_op) as mock_apps,
        patch("src.clean.runner.clean_developer_tools", side_effect=no_op) as mock_dev,
        patch("src.clean.runner.ScanCache.clear"),
    ):
        result = runner.run_clean(dry_run=False)

    assert result is False
    mock_sudo.assert_not_called()
    mock_system.assert_not_called()
    mock_user.assert_not_called()
    mock_apps.assert_not_called()
    mock_dev.assert_not_called()


def test_clean_sudo_cancel_prompt_has_no_trailing_blank_line():
    with (
        patch("builtins.print") as mock_print,
        patch("src.core.terminal_state.read_sudo_choice", return_value="\r"),
        patch("src.core.system.ensure_sudo_session", return_value=False),
        patch("src.clean.runner.proactive_app_detection", return_value={}),
        patch.object(runner.system, "SUDO_CANCELLED", True),
    ):
        assert runner.run_clean(dry_run=False) is False

    cancel_calls = [
        call
        for call in mock_print.call_args_list
        if call.args and "Cleanup cancelled" in call.args[0]
    ]
    assert cancel_calls
    # One line break, and it comes from `end`: the notice used to print with
    # end="" and nothing after it, which put the shell prompt on the same line.
    assert "\n" not in cancel_calls[-1].args[0]
    assert cancel_calls[-1].kwargs["end"] == "\n"


def test_optimize_sudo_cancel_prompt_has_no_trailing_blank_line():
    with (
        patch("builtins.print") as mock_print,
        patch("src.optimize.os.system"),
        patch("src.core.terminal_state.read_sudo_choice", return_value="\r"),
        patch("src.core.system.ensure_sudo_session", return_value=False),
        patch.object(optimize.system, "SUDO_CANCELLED", True),
    ):
        assert optimize.optimize_system(dry_run=False) is False

    cancel_calls = [
        call
        for call in mock_print.call_args_list
        if call.args and "Optimization cancelled" in call.args[0]
    ]
    assert cancel_calls
    assert "\n" not in cancel_calls[-1].args[0]
    assert cancel_calls[-1].kwargs["end"] == "\n"
