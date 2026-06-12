import signal

import pytest

from src.core import terminal_state


def _reset_terminal_state():
    terminal_state._raw_states.clear()
    terminal_state._alternate_screen_depth = 0
    terminal_state._cursor_hidden = False
    terminal_state._mouse_tracking_enabled = False


def test_reset_terminal_is_silent_when_no_terminal_state_is_active(monkeypatch):
    _reset_terminal_state()
    writes = []
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
