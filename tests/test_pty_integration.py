"""Pseudo-terminal integration for the TUI, signals and terminal restoration.

The rest of the UI suite drives the navigator with mocked raw-mode and a fake
stdin; the signal suite calls the handlers directly. Neither proves the parts
that exist only on a real terminal: that install_signal_handlers() actually
arms SIGINT/SIGTERM, that a real Ctrl-C resets the terminal and exits 130, and
that the menu enters and leaves the alternate screen for real. These tests run
topo under a real pty, so those escape sequences, that termios raw mode and
that signal delivery are exercised end to end rather than stubbed.

They launch ``python -m src.main`` with no arguments -- the main menu, which
renders immediately and reads one key, so there is no scan to wait on and no
engine dependency. The menu runs in the child, so coverage does not count
src/ui here (the UI executes out of process); that is why the ``src/ui/**``
omit in pyproject stays until a subprocess-coverage harness exists.
"""

import fcntl
import os
import pty
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
from contextlib import suppress
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="topo is Linux-only; pty/termios drive this suite"
)

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The escape sequences terminal_state emits. Asserting on the bytes is the whole
# point: this is the layer the mocked UI tests cannot see.
_ALT_SCREEN_ON = b"\x1b[?1049h"
_ALT_SCREEN_OFF = b"\x1b[?1049l"
_SHOW_CURSOR = b"\x1b[?25h"
_MENU_MARKER = b"Main Menu"


class _TopoPTY:
    """A topo process on the slave end of a pty, read from the master end."""

    def __init__(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="topo_pty_home_"))
        self.master, slave = pty.openpty()
        # Pin an 80x24 window so get_terminal_size() is stable and the key
        # reader never sees a phantom RESIZE between frames.
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        env = dict(os.environ)
        env["HOME"] = str(self.home)
        # Keep the run hermetic: anything stateful must land under the temp HOME,
        # not a developer shell's XDG dirs.
        for leaky in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
            env.pop(leaky, None)
        env["COLUMNS"], env["LINES"] = "80", "24"
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "src.main"],
            stdin=slave,
            stdout=slave,
            stderr=subprocess.PIPE,
            cwd=_REPO_ROOT,
            env=env,
            close_fds=True,
        )
        # The child owns the slave now; the parent reads/writes only the master.
        os.close(slave)
        os.set_blocking(self.master, False)

    def read_until(self, marker: bytes, timeout: float = 20.0) -> tuple[bytes, bool]:
        """Read the master until *marker* appears or *timeout* elapses."""
        buf = b""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                chunk = os.read(self.master, 4096)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            except OSError:
                break  # the slave closed: the child has exited
            if chunk:
                buf += chunk
                if marker in buf:
                    return buf, True
        return buf, False

    def drain(self, settle: float = 1.5) -> bytes:
        """Collect whatever the child writes next -- the reset sequences on exit."""
        buf = b""
        deadline = time.time() + settle
        while time.time() < deadline:
            try:
                chunk = os.read(self.master, 4096)
            except BlockingIOError:
                time.sleep(0.02)
                continue
            except OSError:
                break
            if chunk:
                buf += chunk
        return buf

    def write(self, data: bytes) -> None:
        os.write(self.master, data)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        with suppress(OSError):
            os.close(self.master)
        if self.proc.stderr:
            self.proc.stderr.close()
        shutil.rmtree(self.home, ignore_errors=True)


@pytest.fixture
def topo_pty():
    pty_proc = _TopoPTY()
    try:
        yield pty_proc
    finally:
        pty_proc.close()


def test_pty_menu_renders_and_a_real_esc_keypress_restores_the_terminal(topo_pty):
    """Render, a real cbreak keypress, and the alternate-screen round trip.

    The mocked UI tests never put a real terminal into raw mode. Here the menu
    paints into the alternate screen, a genuine ESC byte is read back in cbreak
    mode and understood as "quit", and leaving drops the alternate screen -- so
    the terminal the user gets back is the one they started with.
    """
    before, rendered = topo_pty.read_until(_MENU_MARKER)
    assert rendered, "the menu never reached the terminal"
    assert _ALT_SCREEN_ON in before
    # Real rendering, not a stub: the menu's own entries reached the pty.
    assert b"Clean" in before and b"Analyze" in before

    topo_pty.write(b"\x1b")  # a lone ESC, read in real cbreak mode -> quit
    after = topo_pty.drain()
    assert topo_pty.proc.wait(timeout=10) == 0
    # The alternate screen is left on the way out, restoring the main buffer.
    assert _ALT_SCREEN_OFF in (before + after)


def test_pty_real_sigint_exits_130_resets_the_terminal_and_notes_the_interrupt(topo_pty):
    """A real SIGINT, not a hand-called handler: the whole chain must fire.

    install_signal_handlers() has to have actually armed SIGINT, _handle_signal
    has to reset the terminal before raising, main() has to turn the
    KeyboardInterrupt into exit 130, and the notice has to land on stderr. Were
    the handler never installed, the default action would kill the child with
    returncode -SIGINT and none of that would hold.
    """
    _, rendered = topo_pty.read_until(_MENU_MARKER)
    assert rendered

    topo_pty.proc.send_signal(signal.SIGINT)
    output = topo_pty.drain()
    assert topo_pty.proc.wait(timeout=10) == 130
    # reset_terminal(force=True) ran on the real signal: the alternate screen is
    # left and the cursor is shown again.
    assert _ALT_SCREEN_OFF in output
    assert _SHOW_CURSOR in output
    # The interrupt notice goes to stderr, so a redirected stdout report stays clean.
    stderr = topo_pty.proc.stderr.read().decode("utf-8", "ignore")
    assert "interrupted" in stderr.lower()


def test_pty_real_sigterm_exits_143_and_resets_the_terminal(topo_pty):
    """SIGTERM is raised as SystemExit(128 + SIGTERM) and still resets the screen.

    The counterpart to the SIGINT path: a real SIGTERM must exit 143 -- not die
    with returncode -SIGTERM, which is what an un-installed handler would give --
    and must leave the alternate screen behind it.
    """
    _, rendered = topo_pty.read_until(_MENU_MARKER)
    assert rendered

    topo_pty.proc.send_signal(signal.SIGTERM)
    output = topo_pty.drain()
    assert topo_pty.proc.wait(timeout=10) == 143
    assert _ALT_SCREEN_OFF in output
