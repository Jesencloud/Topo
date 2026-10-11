import atexit
import contextlib
import select
import signal
import sys
import termios
import tty
from collections.abc import Callable
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
_cursor_hidden_depth = 0
_cursor_hidden = False
_mouse_tracking_enabled = False

# Callbacks run at the very start of reset_terminal(), before any termios restore
# or escape write. A background writer (the spinner's daemon thread) registers one
# so a reset -- which on Ctrl-C runs from the signal handler, before the spinner's
# own context-manager finally can stop it -- halts the writer first, instead of
# letting it wake from its interval sleep and paint one more frame onto the screen
# we just restored. Kept here, not imported from spinner, so the dependency points
# the right way (spinner -> terminal_state).
_pre_reset_hooks: list[Callable[[], None]] = []


def register_reset_hook(hook: Callable[[], None]) -> None:
    _pre_reset_hooks.append(hook)


def unregister_reset_hook(hook: Callable[[], None]) -> None:
    with contextlib.suppress(ValueError):
        _pre_reset_hooks.remove(hook)


def _write(sequence: str) -> None:
    # Every escape this module emits goes through here, so this is the one place
    # that has to ask whether there is a terminal to emit them to. Redirecting
    # stdout used to put the whole reset sequence -- mouse-off, cursor-on,
    # graphics-reset, alt-screen-off -- into the log on Ctrl-C, on top of the
    # \033[2J that followed it. The depth counters above are kept up to date
    # either way: they track what topo *asked for*, not what reached a screen,
    # so a run that starts redirected and is inspected later still reads right.
    try:
        if not sys.stdout.isatty():
            return
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
    # Only the outermost enter actually switches the buffer. Nested spans (e.g. the
    # TUI menu holding the alt-screen while it launches an alternate-screen action)
    # must NOT re-emit \x1b[?1049h: toggling the buffer off then on flashes the main
    # buffer -- the shell prompt -- for a frame between the two spans.
    global _alternate_screen_depth
    _alternate_screen_depth += 1
    if _alternate_screen_depth == 1:
        _write(ENTER_ALTERNATE_SCREEN)


def exit_alternate_screen() -> None:
    # Mirror enter: only leave the alt-screen once the outermost span closes, so an
    # inner span ending never drops back to the main buffer mid-session.
    global _alternate_screen_depth
    if _alternate_screen_depth <= 0:
        return
    _alternate_screen_depth -= 1
    if _alternate_screen_depth == 0:
        _write(EXIT_ALTERNATE_SCREEN)


def hide_cursor() -> None:
    global _cursor_hidden, _cursor_hidden_depth
    _cursor_hidden_depth += 1
    _cursor_hidden = True
    if _cursor_hidden_depth == 1:
        _write(HIDE_CURSOR)


def show_cursor() -> None:
    global _cursor_hidden, _cursor_hidden_depth
    if _cursor_hidden_depth <= 0:
        return
    _cursor_hidden_depth -= 1
    _cursor_hidden = _cursor_hidden_depth > 0
    if _cursor_hidden_depth == 0:
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
    global _alternate_screen_depth, _cursor_hidden, _cursor_hidden_depth, _mouse_tracking_enabled

    # Stop any registered background writers first (see _pre_reset_hooks), so none
    # repaints between the restore below and the caller's own cleanup. A hook must
    # never break the reset itself.
    for hook in list(_pre_reset_hooks):
        with contextlib.suppress(Exception):
            hook()

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
    _cursor_hidden_depth = 0
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


def read_sudo_choice() -> str:
    """Read a single-key sudo confirmation from the terminal.

    Returns the key character: Enter/Space for accept, ESC for cancel.
    Shared helper extracted from runner.py and optimize.py.
    """
    if not sys.stdin.isatty():
        return "\n"

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    remember_raw_state(fd, old_settings)
    try:
        tty.setraw(fd)
        while True:
            choice = sys.stdin.read(1)
            if choice == "\x03":
                # Raw mode turns Ctrl+C into a byte instead of SIGINT. Restore
                # the terminal in finally, then let the caller cancel cleanly.
                raise KeyboardInterrupt
            if choice in ("\r", "\n", " "):
                return choice
            if choice == "\x1b":
                # Arrow/function keys start with ESC. Treat only a lone ESC as cancel.
                ready, _, _ = select.select([sys.stdin], [], [], 0.05)
                if not ready:
                    return choice
                while ready:
                    sys.stdin.read(1)
                    ready, _, _ = select.select([sys.stdin], [], [], 0)
    finally:
        restore_raw_state(fd, old_settings)


def _handle_signal(signum: int, frame: FrameType | None) -> None:
    reset_terminal(force=True)
    previous = _previous_handlers.get(signum)
    if callable(previous) and previous not in (_handle_signal, signal.default_int_handler):
        previous(signum, frame)
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)
