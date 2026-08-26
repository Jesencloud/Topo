import signal

import pytest

from src.core import terminal_state


def _reset_terminal_state():
    terminal_state._raw_states.clear()
    terminal_state._alternate_screen_depth = 0
    terminal_state._cursor_hidden_depth = 0
    terminal_state._cursor_hidden = False
    terminal_state._mouse_tracking_enabled = False


def test_reset_terminal_is_silent_when_no_terminal_state_is_active(monkeypatch):
    _reset_terminal_state()
    writes = []
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.write", writes.append)
    monkeypatch.setattr("sys.stdout.flush", lambda: None)

    terminal_state.reset_terminal()

    assert writes == []


def test_signal_handler_resets_terminal_before_interrupt(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal_state, "reset_terminal", lambda force=False: calls.append(force))
    monkeypatch.setitem(
        terminal_state._previous_handlers, signal.SIGINT, signal.default_int_handler
    )

    with pytest.raises(KeyboardInterrupt):
        terminal_state._handle_signal(signal.SIGINT, None)

    assert calls == [True]


def test_signal_handler_resets_terminal_before_termination(monkeypatch):
    calls = []
    monkeypatch.setattr(terminal_state, "reset_terminal", lambda force=False: calls.append(force))
    monkeypatch.setitem(terminal_state._previous_handlers, signal.SIGTERM, signal.SIG_DFL)

    with pytest.raises(SystemExit) as exc:
        terminal_state._handle_signal(signal.SIGTERM, None)

    assert exc.value.code == 128 + signal.SIGTERM
    assert calls == [True]


def _capture_writes(monkeypatch):
    writes = []
    # These tests are about what topo emits *to a terminal*; _write() drops every
    # sequence when stdout is redirected, and under pytest it always is.
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.write", writes.append)
    monkeypatch.setattr("sys.stdout.flush", lambda: None)
    return writes


def test_no_escape_sequences_reach_a_redirected_stdout(monkeypatch):
    # Ctrl-C on `topo optimize > log` used to append the whole reset sequence --
    # mouse-off, cursor-on, graphics-reset, alt-screen-off -- to the log.
    _reset_terminal_state()
    writes = []
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.write", writes.append)
    monkeypatch.setattr("sys.stdout.flush", lambda: None)

    terminal_state.enter_alternate_screen()
    terminal_state.hide_cursor()
    terminal_state.enable_mouse_tracking()
    terminal_state.reset_terminal(force=True)

    assert writes == []
    # The bookkeeping still ran, so a later reset has nothing left to undo.
    assert terminal_state._alternate_screen_depth == 0
    assert terminal_state._cursor_hidden is False
    assert terminal_state._mouse_tracking_enabled is False


def test_nested_enter_does_not_retoggle_the_alternate_buffer(monkeypatch):
    # A nested span (the TUI menu launching an alternate-screen action while it
    # already holds the alt-screen) must not switch the buffer off and on -- that
    # is exactly the frame where the shell prompt flashes through.
    _reset_terminal_state()
    writes = _capture_writes(monkeypatch)

    terminal_state.enter_alternate_screen()  # outermost: switches to alt
    terminal_state.enter_alternate_screen()  # nested: no-op on the buffer
    terminal_state.exit_alternate_screen()  # inner span closes: still in alt

    assert writes == [terminal_state.ENTER_ALTERNATE_SCREEN]
    assert terminal_state._alternate_screen_depth == 1


def test_outermost_exit_leaves_the_alternate_buffer(monkeypatch):
    _reset_terminal_state()
    terminal_state.enter_alternate_screen()
    terminal_state.enter_alternate_screen()
    terminal_state.exit_alternate_screen()
    writes = _capture_writes(monkeypatch)

    terminal_state.exit_alternate_screen()  # outermost span closes

    assert writes == [terminal_state.EXIT_ALTERNATE_SCREEN]
    assert terminal_state._alternate_screen_depth == 0


def test_exit_without_active_alternate_screen_is_silent(monkeypatch):
    _reset_terminal_state()
    writes = _capture_writes(monkeypatch)

    terminal_state.exit_alternate_screen()

    assert writes == []
    assert terminal_state._alternate_screen_depth == 0


def test_nested_cursor_hide_does_not_flash_visible_between_views(monkeypatch):
    _reset_terminal_state()
    writes = _capture_writes(monkeypatch)

    terminal_state.hide_cursor()  # outer menu span
    terminal_state.hide_cursor()  # selector span
    terminal_state.show_cursor()  # selector closes: remain hidden

    assert writes == [terminal_state.HIDE_CURSOR]
    assert terminal_state._cursor_hidden_depth == 1
    assert terminal_state._cursor_hidden is True

    terminal_state.show_cursor()
    assert writes == [terminal_state.HIDE_CURSOR, terminal_state.SHOW_CURSOR]
