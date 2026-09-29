"""Shared threaded spinner lifecycle for long-running terminal operations."""

import sys
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

DEFAULT_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")


@contextmanager
def threaded_spinner(
    render: Callable[[str], None],
    *,
    frames: tuple[str, ...] = DEFAULT_SPINNER_FRAMES,
    interval: float = 0.08,
) -> Iterator[None]:
    """Run ``render`` on a daemon thread until the context exits.

    The callback owns all terminal output and synchronization. This helper only
    centralizes frame selection, cooperative stopping, and thread cleanup.

    Nothing is rendered at all when stdout is not a terminal. An animation needs
    a cursor to rewind: with the output redirected, `topo optimize > log` used to
    append a fresh frame line a dozen times a second for the whole run. Every
    caller's real progress -- optimize's per-task rows, the uninstall screen's
    per-app lines -- is printed outside the spinner, so a redirected run loses
    only the animation.
    """
    if not frames:
        raise ValueError("spinner frames must not be empty")

    try:
        is_terminal = sys.stdout.isatty()
    except (OSError, ValueError):
        is_terminal = False
    if not is_terminal:
        yield
        return

    stop = threading.Event()
    # A render callback that raises would otherwise kill this daemon thread
    # without a word: the animation freezes on its last frame and nothing says
    # why. Catch it, stop the loop cleanly, and keep the first exception so the
    # context exit can surface it. The spinner is decorative -- a broken render
    # must not take down the work it was animating -- but it must not vanish
    # either, or a real stdout error hides behind a frozen cursor.
    render_error: list[Exception] = []

    def animate() -> None:
        frame_index = 0
        while not stop.is_set():
            try:
                render(frames[frame_index % len(frames)])
            except Exception as exc:
                render_error.append(exc)
                stop.set()
                break
            frame_index += 1
            stop.wait(interval)

    thread = threading.Thread(target=animate, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=max(interval * 2, 0.2))
        # Diagnosable, not fatal: one line to stderr (stdout is the animation's
        # own surface and may be mid-frame), and only when the body did not
        # itself raise -- an exception propagating out of the `yield` is the real
        # error, and this must not talk over it. join() above ran to completion,
        # so render_error is fully written before it is read here.
        if render_error and sys.exc_info()[1] is None:
            first = render_error[0]
            print(
                f"topo: spinner render failed ({type(first).__name__}: {first})",
                file=sys.stderr,
            )
