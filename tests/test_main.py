from unittest.mock import patch

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


def test_doctor_command_routes_to_run_doctor():
    with (
        patch("sys.argv", ["topo", "doctor"]),
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch("src.main.run_doctor") as run_doctor,
    ):
        topo_main.main()

    run_doctor.assert_called_once_with()
