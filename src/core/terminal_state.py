import atexit
import signal
import sys
import termios
from types import FrameType
from typing import Any

MOUSE_DISABLE = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l"
MOUSE_ENABLE = "\x1b[?1000h\x1b[?1002h\x1b[?1006h"
ENTER_ALTERNATE_SCREEN = "\x1b[?1049h\x1b[H"
EXIT_ALTERNATE_SCREEN = "\x1b[?1049l"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"
RESET_GRAPHICS = "\x1b[0m"

_installed = False
_previous_handlers: dict[int, Any] = {}
_raw_states: list[tuple[int, Any]] = []
_alternate_screen_depth = 0
_cursor_hidden = False
_mouse_tracking_enabled = False


def _write(sequence: str) -> None:
    try:
        sys.stdout.write(sequence)
        sys.stdout.flush()
    except (OSError, ValueError):
        return


def _restore_termios(fd: int, settings: Any) -> None:
    try:
        termios.tcsetattr(fd, termios.TCSADRAIN, settings)
    except (termios.error, OSError):
        return


def remember_raw_state(fd: int, settings: Any) -> None:
    _raw_states.append((fd, settings))


def restore_raw_state(fd: int, settings: Any) -> None:
    for index in range(len(_raw_states) - 1, -1, -1):
        if _raw_states[index][0] == fd:
            del _raw_states[index]
            break
    _restore_termios(fd, settings)


def enter_alternate_screen() -> None:
    global _alternate_screen_depth
    _alternate_screen_depth += 1
    _write(ENTER_ALTERNATE_SCREEN)


def exit_alternate_screen() -> None:
    global _alternate_screen_depth
    if _alternate_screen_depth > 0:
        _alternate_screen_depth -= 1
    _write(EXIT_ALTERNATE_SCREEN)


def hide_cursor() -> None:
    global _cursor_hidden
    _cursor_hidden = True
    _write(HIDE_CURSOR)


def show_cursor() -> None:
    global _cursor_hidden
    _cursor_hidden = False
    _write(SHOW_CURSOR)


def enable_mouse_tracking() -> None:
    global _mouse_tracking_enabled
    _mouse_tracking_enabled = True
    _write(MOUSE_ENABLE)


def disable_mouse_tracking() -> None:
    global _mouse_tracking_enabled
    _mouse_tracking_enabled = False
    _write(MOUSE_DISABLE)


def reset_terminal(force: bool = False) -> None:
    global _alternate_screen_depth, _cursor_hidden, _mouse_tracking_enabled

    active = (
        bool(_raw_states)
        or _alternate_screen_depth > 0
        or _cursor_hidden
        or _mouse_tracking_enabled
    )
    if not force and not active:
        return

    for fd, settings in reversed(_raw_states):
        _restore_termios(fd, settings)
    _raw_states.clear()

    _write(MOUSE_DISABLE + SHOW_CURSOR + RESET_GRAPHICS + EXIT_ALTERNATE_SCREEN)
    _alternate_screen_depth = 0
    _cursor_hidden = False
    _mouse_tracking_enabled = False


def install_signal_handlers() -> None:
    global _installed
    if _installed:
        return

    atexit.register(reset_terminal)
    for signum in (signal.SIGINT, signal.SIGTERM):
        _previous_handlers[signum] = signal.getsignal(signum)
        signal.signal(signum, _handle_signal)
    _installed = True


def _handle_signal(signum: int, frame: FrameType | None) -> None:
    reset_terminal(force=True)
    previous = _previous_handlers.get(signum)
    if callable(previous) and previous not in (_handle_signal, signal.default_int_handler):
        previous(signum, frame)
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)
