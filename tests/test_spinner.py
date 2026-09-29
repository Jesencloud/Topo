import threading
from unittest.mock import patch

import pytest

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


def test_threaded_spinner_survives_a_render_that_raises(capsys):
    """A broken render callback must not take down the work it animates.

    The render runs on a daemon thread; before this it could raise and kill that
    thread with no trace -- the spinner froze and nothing said why. Now the loop
    stops cleanly, the `with` body runs to completion, and one diagnostic reaches
    stderr so the failure is at least visible.
    """
    entered_render = threading.Event()

    def bad_render(frame):
        entered_render.set()
        raise RuntimeError("render boom")

    body_ran = False
    with (
        patch("src.core.spinner.sys.stdout.isatty", return_value=True),
        threaded_spinner(bad_render, frames=("x",), interval=0.001),
    ):
        assert entered_render.wait(1)
        body_ran = True

    assert body_ran
    err = capsys.readouterr().err
    assert "spinner render failed" in err
    assert "RuntimeError" in err


def test_threaded_spinner_render_error_stays_quiet_when_the_body_raises(capsys):
    """The body's own exception is the real error; the render diagnostic must not
    talk over it, so a render failure during a failing body is swallowed."""
    entered_render = threading.Event()

    def bad_render(frame):
        entered_render.set()
        raise RuntimeError("render boom")

    with (
        pytest.raises(ValueError, match="body boom"),
        patch("src.core.spinner.sys.stdout.isatty", return_value=True),
        threaded_spinner(bad_render, frames=("x",), interval=0.001),
    ):
        assert entered_render.wait(1)
        raise ValueError("body boom")

    assert "spinner render failed" not in capsys.readouterr().err
