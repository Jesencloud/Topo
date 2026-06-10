from unittest.mock import patch

from src import main as topo_main


def test_run_terminal_tui_command_waits_after_output():
    def command(label):
        print(f"finished {label}")

    with (
        patch("src.main.Navigator.wait_for_return", return_value=False) as wait_for_return,
        patch("builtins.print") as print_mock,
    ):
        assert topo_main._run_terminal_tui_command(command, "clean") is False

    print_mock.assert_called_once_with("finished clean")
    wait_for_return.assert_called_once_with()


def test_run_terminal_tui_command_skips_wait_when_command_returns_false():
    def command():
        print("skipped")
        return False

    with (
        patch("src.main.Navigator.wait_for_return") as wait_for_return,
        patch("builtins.print"),
    ):
        assert topo_main._run_terminal_tui_command(command) is True

    wait_for_return.assert_not_called()
