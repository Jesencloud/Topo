import threading
from unittest.mock import patch

from src.core.spinner import threaded_spinner


def test_threaded_spinner_renders_and_stops_cleanly():
    rendered = threading.Event()
    frames = []

    def render(frame):
        frames.append(frame)
        rendered.set()

    with (
        patch("src.core.spinner.sys.stdout.isatty", return_value=True),
        threaded_spinner(render, frames=("x",), interval=0.001),
    ):
        assert rendered.wait(1)

    assert frames
    assert set(frames) == {"x"}


def test_threaded_spinner_does_not_animate_without_a_terminal():
    # An animation rewrites one line; with stdout redirected there is no cursor to
    # rewind, so every frame would be appended to the log instead.
    frames = []

    with (
        patch("src.core.spinner.sys.stdout.isatty", return_value=False),
        threaded_spinner(frames.append, frames=("x",), interval=0.001),
    ):
        threading.Event().wait(0.05)

    assert frames == []
