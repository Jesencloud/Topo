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
        patch("src.main.SingleInstanceLock", return_value=nullcontext()),
        patch("src.main.main_menu", return_value="clean"),
        patch("src.main._run_terminal_tui_command", return_value=False) as run_terminal,
    ):
        topo_main.main()

    run_terminal.assert_called_once_with(topo_main.run_clean, False)


def test_screen_helpers_and_interrupted_output(capsys):
    with patch("src.main.sys.stdout.write") as write, patch("src.main.sys.stdout.flush") as flush:
        topo_main._clear_screen()
        topo_main._print_interrupted(clear_screen=False)
    assert write.call_count >= 2
    flush.assert_called()


def test_alternate_tui_runs_command_inside_screen():
    with (
        patch("src.main.alternate_screen", return_value=nullcontext()),
        patch("src.main._clear_screen") as clear,
        patch("src.main.run_deep_analysis", return_value="done"),
    ):
        result = topo_main._run_alternate_tui(lambda: "ok")
    assert result == "ok"
    clear.assert_called_once_with()


@pytest.mark.parametrize(
    ("argv", "target", "expected"),
    [
        (["topo", "clean", "--dry-run"], "run_clean", (True,)),
        (["topo", "optimize"], "optimize_system", (False,)),
        (["topo", "status"], "show_status", ()),
        (["topo", "history", "--limit", "0"], "show_history", (1,)),
        (["topo", "update"], "run_update", ()),
        (["topo", "remove", "--dry-run"], "run_remove", (True,)),
    ],
)
def test_cli_routes_commands(argv, target, expected):
    patches = {
        "run_clean": patch("src.main.run_clean"),
        "optimize_system": patch("src.main.optimize_system"),
        "show_status": patch("src.main.show_status"),
        "show_history": patch("src.main.show_history"),
        "run_update": patch("src.main.run_update"),
        "run_remove": patch("src.main.run_remove"),
    }
    with (
        patch("sys.argv", argv),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch("src.main.SingleInstanceLock", return_value=nullcontext()),
        patches[target] as mocked,
    ):
        topo_main.main()
    if target == "show_history":
        mocked.assert_called_once_with(limit=expected[0])
    else:
        mocked.assert_called_once_with(*expected)


def test_cli_authorize_link_failure_and_analyze_uninstall_routes():
    with (
        patch("sys.argv", ["topo", "authorize"]),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.system.setup_passwordless_sudo") as setup,
    ):
        topo_main.main()
    setup.assert_called_once_with()

    with (
        patch("sys.argv", ["topo", "link"]),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch("src.main.run_install_link", return_value=False),
        pytest.raises(SystemExit) as exc,
    ):
        topo_main.main()
    assert exc.value.code == 1

    for command, function in (("analyze", "run_deep_analysis"), ("uninstall", "run_uninstall")):
        with (
            patch("sys.argv", ["topo", command]),
            patch("src.main.terminal_state.install_signal_handlers"),
            patch("src.main.alternate_screen", return_value=nullcontext()),
            patch("src.main.SingleInstanceLock", return_value=nullcontext()),
            patch(f"src.main.{function}") as mocked,
        ):
            topo_main.main()
        mocked.assert_called_once_with()


def test_whitelist_cli_success_duplicate_remove_failure_and_list(capsys):
    cases = [
        (["topo", "whitelist", "add", "/tmp/x"], "add_to_whitelist", True),
        (["topo", "whitelist", "add", "/tmp/x"], "add_to_whitelist", False),
        (["topo", "whitelist", "remove", "/tmp/x"], "remove_from_whitelist", True),
    ]
    for argv, function, result in cases:
        with (
            patch("sys.argv", argv),
            patch("src.main.terminal_state.install_signal_handlers"),
            patch(f"src.main.{function}", return_value=result) as mocked,
        ):
            topo_main.main()
        mocked.assert_called_once_with("/tmp/x")
    with (
        patch("sys.argv", ["topo", "whitelist", "remove", "/tmp/x"]),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.remove_from_whitelist", return_value=False),
        pytest.raises(SystemExit) as exc,
    ):
        topo_main.main()
    assert exc.value.code == 1
    with (
        patch("sys.argv", ["topo", "whitelist"]),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.core.whitelist.get_whitelist", return_value=[]),
    ):
        topo_main.main()
    assert "(Empty)" in capsys.readouterr().out


def test_whitelist_argument_errors():
    for argv in (["topo", "whitelist", "add"], ["topo", "whitelist", "list", "/tmp/x"]):
        with (
            patch("sys.argv", argv),
            patch("src.main.terminal_state.install_signal_handlers"),
            pytest.raises(SystemExit) as exc,
        ):
            topo_main.main()
        assert exc.value.code == 2


def test_main_screen_context_restores_alternate_screen():
    with (
        patch("src.main.terminal_state.enter_alternate_screen") as enter,
        patch("src.main.terminal_state.exit_alternate_screen") as exit_screen,
        topo_main.main_screen(),
    ):
        pass
    exit_screen.assert_called_once_with()
    enter.assert_called_once_with()
