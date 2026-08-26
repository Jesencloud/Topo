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

    def animate() -> None:
        frame_index = 0
        while not stop.is_set():
            render(frames[frame_index % len(frames)])
            frame_index += 1
            stop.wait(interval)

    thread = threading.Thread(target=animate, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=max(interval * 2, 0.2))
