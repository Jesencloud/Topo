import threading

from src.core.spinner import threaded_spinner


def test_threaded_spinner_renders_and_stops_cleanly():
    rendered = threading.Event()
    frames = []

    def render(frame):
        frames.append(frame)
        rendered.set()

    with threaded_spinner(render, frames=("x",), interval=0.001):
        assert rendered.wait(1)

    assert frames
    assert set(frames) == {"x"}
