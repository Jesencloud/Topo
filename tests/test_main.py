from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

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


def test_run_terminal_tui_command_keeps_its_output_on_interrupt():
    """A report-style command interrupted from the menu keeps what it printed.

    main_screen() dropped to the main buffer precisely so this command's output
    would survive in the scrollback; clearing the screen on the way out undid
    that, which is the whole point of not doing it any more. The one remaining
    _clear_screen() call is the one *before* the command runs.
    """

    def command():
        raise KeyboardInterrupt

    with (
        patch("src.main._clear_screen") as clear_screen,
        patch("src.main.terminal_state.reset_terminal") as reset_terminal,
        patch("src.main.Navigator.wait_for_return") as wait_for_return,
        patch("builtins.print") as print_mock,
    ):
        assert topo_main._run_terminal_tui_command(command) is False

    clear_screen.assert_called_once_with()
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


def test_startup_passes_the_configured_theme_color(test_env):
    # The title color is resolved once, before the version banner, so every
    # command honours config.json without any of them knowing about it.
    with (
        patch("sys.argv", ["topo", "doctor"]),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch("src.main.run_doctor"),
        patch("src.main.get_theme_color", return_value="cyan"),
        patch("src.main.setup_color_mode") as setup_color_mode,
    ):
        topo_main.main()

    setup_color_mode.assert_called_once_with(False, theme_color="cyan")


def test_main_keeps_direct_command_output_on_interrupt():
    """Ctrl-C on a CLI command restores the terminal without erasing the report.

    A direct `topo status` / `topo clean` never entered the alternate screen, so
    there is no frame to tidy -- only the command's own output, which is what the
    user needs after interrupting a run that deletes things.
    """
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
    clear_screen.assert_not_called()
    assert print_mock.call_args_list[-1].args == (topo_main.INTERRUPTED_MESSAGE,)


def test_main_menu_clean_action_routes_to_clean():
    # Also pins the menu half of the exit-code contract: the menu is a session,
    # not an operation, so an action that returned False (a cleanup that declined
    # sudo) ends the loop without making `topo` itself exit non-zero.
    with (
        patch("sys.argv", ["topo"]),
        # Both halves of the terminal guard: keys to read and a screen to draw on.
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdout.isatty", return_value=True),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.SingleInstanceLock", return_value=nullcontext()),
        patch("src.main.main_menu", return_value="clean"),
        patch("src.main._run_terminal_tui_command", return_value=False) as run_terminal,
    ):
        topo_main.main()

    run_terminal.assert_called_once_with(topo_main.run_clean, False)


def test_menu_reopens_on_the_entry_just_run():
    # The highlighted entry is derived from the route table's own order, so a
    # route inserted in the wrong place would move the cursor to another entry.
    with (
        patch("src.main.alternate_screen", return_value=nullcontext()),
        patch("src.main.terminal_state.hide_cursor"),
        patch("src.main.terminal_state.show_cursor"),
        patch("src.main._run_alternate_tui", return_value=None),
        patch(
            "src.main.main_menu", side_effect=["analyze", topo_main.QUIT_ACTION]
        ) as main_menu_mock,
    ):
        topo_main._run_menu_loop(False)

    assert [call.args[0] for call in main_menu_mock.call_args_list] == [0, 3]


@pytest.mark.parametrize(
    ("argv", "target", "label"),
    [
        (["topo"], "main_menu", "The topo menu"),
        (["topo", "analyze"], "run_deep_analysis", "topo analyze"),
        (["topo", "uninstall"], "run_uninstall", "topo uninstall"),
    ],
)
def test_interactive_commands_refuse_a_non_tty_instead_of_crashing(argv, target, label, capsys):
    # All three drive Navigator.raw_mode(), whose termios.tcgetattr(stdin) raises
    # termios.error -- not an OSError subclass -- whenever stdin is not a
    # terminal (`echo | topo`, `topo analyze < /dev/null`, cron), so nothing on
    # the way up caught it and the launcher printed a traceback. The guard must
    # also fire before the lock is taken: refusing is not a reason to fight
    # another instance for it.
    with (
        patch("sys.argv", argv),
        patch("sys.stdin.isatty", return_value=False),
        patch("sys.stdout.isatty", return_value=False),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.SingleInstanceLock") as lock_class,
        # QUIT_ACTION so a regressed guard fails on the assertions below instead
        # of spinning forever in the menu loop on an unroutable MagicMock.
        patch(f"src.main.{target}", return_value=topo_main.QUIT_ACTION) as mocked,
        # Refusing to run is a failure, not a quiet no-op: exit 1 so a script
        # that pipes into topo can tell it never did the work.
        pytest.raises(SystemExit) as exit_info,
    ):
        topo_main.main()
    assert exit_info.value.code == 1

    # A refusal is not part of the report: it goes to stderr, so `topo > file`
    # leaves the shell noisy and the file empty rather than the other way round.
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"{label} needs an interactive terminal" in captured.err
    assert "no terminal to read keys from" in captured.err
    assert "topo clean --dry-run" in captured.err
    mocked.assert_not_called()
    lock_class.assert_not_called()


def test_interactive_commands_refuse_a_redirected_screen_even_with_a_keyboard(capsys):
    """`topo analyze | cat` has a keyboard but nowhere to draw.

    Guarding stdin alone let this through: raw_mode() succeeded, the selector
    answered keys, and the whole alternate-screen frame went down the pipe while
    the user deleted files blind -- and the run still exited 0.
    """
    with (
        patch("sys.argv", ["topo", "analyze"]),
        patch("sys.stdin.isatty", return_value=True),
        patch("sys.stdout.isatty", return_value=False),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.SingleInstanceLock") as lock_class,
        patch("src.main.run_deep_analysis") as analyze,
        pytest.raises(SystemExit) as exit_info,
    ):
        topo_main.main()

    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "output is redirected" in captured.err
    analyze.assert_not_called()
    lock_class.assert_not_called()


def test_screen_helpers_and_interrupted_output(capsys):
    with patch("src.main.sys.stdout.write") as write, patch("src.main.sys.stdout.flush") as flush:
        topo_main._clear_screen()
        topo_main._print_interrupted()
    assert write.call_count >= 2
    flush.assert_called()


def test_interrupt_keeps_what_is_already_on_the_screen(capsys):
    """Ctrl-C must not clear the screen: it is the record of what was deleted.

    reset_terminal() has already dropped back to the main buffer by the time this
    runs, so a CLEAR_SCREEN here would erase the cleanup report -- and the shell
    scrollback under it -- rather than the TUI frame it looks like it is tidying.
    """
    print("✓ Removed 1.2 GB of package caches")

    with patch("src.main.terminal_state.reset_terminal") as reset:
        topo_main._print_interrupted()

    reset.assert_called_once_with(force=True)
    output = capsys.readouterr().out
    assert "✓ Removed 1.2 GB of package caches" in output
    assert topo_main.INTERRUPTED_MESSAGE in output
    assert "\033[2J" not in output


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
        (["topo", "remove", "--dry-run"], "run_remove", (True, False)),
        (["topo", "remove", "--yes"], "run_remove", (False, True)),
        (["topo", "remove", "-y", "--dry-run"], "run_remove", (True, True)),
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


@pytest.mark.parametrize(
    ("argv", "target"),
    [
        (["topo", "clean"], "run_clean"),
        (["topo", "update"], "run_update"),
        (["topo", "remove", "--dry-run"], "run_remove"),
    ],
)
def test_installation_mutating_commands_hold_the_single_instance_lock(argv, target):
    # update replaces ~/.topo through install.sh and remove deletes it outright,
    # so both must be as exclusive as clean is -- a concurrent run would pull the
    # program files out from under whichever instance started first.
    lock = MagicMock()
    with (
        patch("sys.argv", argv),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch("src.main.SingleInstanceLock", return_value=lock) as lock_class,
        patch(f"src.main.{target}"),
    ):
        topo_main.main()

    lock_class.assert_called_once_with()
    lock.__enter__.assert_called_once_with()
    lock.__exit__.assert_called_once()


def test_a_held_lock_becomes_exit_code_one_with_nothing_on_stdout(capsys):
    """`topo clean` while another instance holds the lock: refuse, exit 1, stay quiet.

    The guard raises LockUnavailable instead of calling sys.exit() so that main()
    keeps a single place where an outcome becomes an exit code; this pins both
    halves of that -- the code, and the fact that the reason lands on stderr so a
    redirected report file stays empty rather than collecting the refusal.
    """
    with (
        patch("sys.argv", ["topo", "clean", "--dry-run"]),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch(
            "src.main.SingleInstanceLock",
            side_effect=topo_main.LockUnavailable("Another instance is running"),
        ),
        patch("src.main.run_clean") as clean,
        pytest.raises(SystemExit) as exit_info,
    ):
        topo_main.main()

    assert exit_info.value.code == 1
    clean.assert_not_called()
    captured = capsys.readouterr()
    assert captured.out == ""


def test_status_does_not_take_the_single_instance_lock():
    with (
        patch("sys.argv", ["topo", "status"]),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch("src.main.SingleInstanceLock") as lock_class,
        patch("src.main.show_status"),
    ):
        topo_main.main()

    lock_class.assert_not_called()


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
            patch("sys.stdin.isatty", return_value=True),
            patch("sys.stdout.isatty", return_value=True),
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


# --- Exit-code contract (see the comment above main() in src/main.py) ---


@pytest.mark.parametrize(
    ("argv", "target"),
    [
        (["topo", "clean"], "run_clean"),
        (["topo", "optimize"], "optimize_system"),
        (["topo", "update"], "run_update"),
        (["topo", "remove", "--yes"], "run_remove"),
        (["topo", "doctor"], "run_doctor"),
        (["topo", "authorize"], "system.setup_passwordless_sudo"),
    ],
)
def test_failing_commands_exit_nonzero(argv, target):
    # Every one of these used to exit 0 no matter what it printed, so
    # `topo update && deploy` deployed after a refused signature check and
    # `topo doctor && run` ran against a dead engine.
    with (
        patch("sys.argv", argv),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch("src.main.SingleInstanceLock", return_value=nullcontext()),
        patch(f"src.main.{target}", return_value=False),
        pytest.raises(SystemExit) as exit_info,
    ):
        topo_main.main()
    assert exit_info.value.code == 1


@pytest.mark.parametrize(
    ("argv", "target", "result"),
    [
        # "Nothing to do" is success: the system is already in the state the
        # caller asked for. One command covers it -- the per-command semantics
        # live in that command's own test module; this only pins the conversion.
        (["topo", "update"], "run_update", True),
        # Report-only commands have no failure to report and return None.
        (["topo", "status"], "show_status", None),
        (["topo", "history"], "show_history", None),
    ],
)
def test_successful_commands_exit_zero(argv, target, result):
    with (
        patch("sys.argv", argv),
        patch("src.main.terminal_state.install_signal_handlers"),
        patch("src.main.system.get_os_id", return_value="test-os"),
        patch("src.main.SingleInstanceLock", return_value=nullcontext()),
        patch(f"src.main.{target}", return_value=result),
    ):
        topo_main.main()


def test_unrouted_command_reports_a_missing_handler(capsys):
    # argparse rejects unknown commands with exit 2, so this only fires for a
    # subparser added without a dispatch entry -- which used to print the banner
    # and exit 0.
    args = SimpleNamespace(command="ghost", limit=10, silent=False, yes=False)
    assert topo_main._execute_main_router(args, False) is False
    assert "has no handler" in capsys.readouterr().err
