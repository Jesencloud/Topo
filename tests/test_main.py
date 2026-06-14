from contextlib import nullcontext
from unittest.mock import patch

import pytest

from src import main as topo_main


def test_run_terminal_tui_command_waits_after_output():
    def command(label):
        print(f"finished {label}")

    with (
        patch("src.main._clear_screen") as clear_screen,
        patch("src.main.Navigator.wait_for_return", return_value=False) as wait_for_return,
        patch("builtins.print") as print_mock,
    ):
        assert topo_main._run_terminal_tui_command(command, "clean") is False

    clear_screen.assert_called_once_with()
    print_mock.assert_called_once_with("finished clean")
    wait_for_return.assert_called_once_with()


def test_run_terminal_tui_command_skips_wait_when_command_returns_false():
    def command():
        print("skipped")
        return False

    with (
        patch("src.main._clear_screen") as clear_screen,
        patch("src.main.Navigator.wait_for_return") as wait_for_return,
        patch("builtins.print"),
    ):
        assert topo_main._run_terminal_tui_command(command) is True

    clear_screen.assert_called_once_with()
    wait_for_return.assert_not_called()


def test_run_terminal_tui_command_cleans_status_screen_on_interrupt():
    def command():
        raise KeyboardInterrupt

    with (
        patch("src.main._clear_screen") as clear_screen,
        patch("src.main.terminal_state.reset_terminal") as reset_terminal,
        patch("src.main.Navigator.wait_for_return") as wait_for_return,
        patch("builtins.print") as print_mock,
    ):
        assert topo_main._run_terminal_tui_command(command) is False

    assert clear_screen.call_count == 2
    reset_terminal.assert_called_once_with(force=True)
    print_mock.assert_called_once_with(topo_main.INTERRUPTED_MESSAGE)
    wait_for_return.assert_not_called()


def test_alternate_screen_exits_on_exception():
    with (
        patch("src.main.terminal_state.enter_alternate_screen") as enter_screen,
        patch("src.main.terminal_state.exit_alternate_screen") as exit_screen,
        pytest.raises(KeyboardInterrupt),
        topo_main.alternate_screen(),
    ):
        raise KeyboardInterrupt

    enter_screen.assert_called_once_with()
    exit_screen.assert_called_once_with()


def test_doctor_command_routes_to_run_doctor():
    with (
        patch("sys.argv", ["topo", "doctor"]),
        patch("src.main.terminal_state.install_signal_handlers") as install_signal_handlers,
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch("src.main.run_doctor") as run_doctor,
    ):
        topo_main.main()

    install_signal_handlers.assert_called_once_with()
    run_doctor.assert_called_once_with()


def test_main_cleans_direct_command_output_on_interrupt():
    with (
        patch("sys.argv", ["topo", "status"]),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.terminal_state.reset_terminal") as reset_terminal,
        patch("src.main._clear_screen") as clear_screen,
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch("src.main.show_status", side_effect=KeyboardInterrupt),
        patch("builtins.print") as print_mock,
        pytest.raises(SystemExit) as exc,
    ):
        topo_main.main()

    assert exc.value.code == 130
    reset_terminal.assert_called_once_with(force=True)
    clear_screen.assert_called_once_with()
    assert print_mock.call_args_list[-1].args == (topo_main.INTERRUPTED_MESSAGE,)


def test_main_menu_clean_action_routes_to_clean():
    with (
        patch("sys.argv", ["topo"]),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.alternate_screen", return_value=nullcontext()),
        patch("src.main.main_menu", return_value="clean"),
        patch("src.main._run_terminal_tui_command", return_value=False) as run_terminal,
    ):
        topo_main.main()

    run_terminal.assert_called_once_with(topo_main.run_clean, False)
