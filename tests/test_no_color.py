import sys
from unittest.mock import patch

import pytest

from src.core.config import DEFAULT_CONFIG
from src.core.constants import setup_color_mode
from src.core.text import display_width


@pytest.fixture(autouse=True)
def _restore_real_color_mode():
    """Re-derive the color mode from the real environment after each test.

    setup_color_mode() now rebinds the color names inside every consumer module
    (that is the point of the --no-color fix), so a test that leaves colors
    forced on would leak escape codes into unrelated tests. The theme name is
    process state too, so it goes back to the default as well.
    """
    yield
    setup_color_mode(no_color=False, theme_color=DEFAULT_CONFIG["theme_color"])


def test_no_color_env_variable(monkeypatch):
    """Test that setting NO_COLOR environment variable disables all ANSI colors."""
    monkeypatch.setenv("NO_COLOR", "1")
    with patch.object(sys.stdout, "isatty", return_value=True):
        setup_color_mode(no_color=False)
        from src.core import constants

        assert constants.BLUE == ""
        assert constants.GREEN == ""
        assert constants.RED == ""
        assert constants.OK == "✓"

    # Restore
    monkeypatch.delenv("NO_COLOR", raising=False)
    with patch.object(sys.stdout, "isatty", return_value=True):
        setup_color_mode(no_color=False)
        assert constants.BLUE != ""
        assert constants.OK != "✓"


def test_no_color_flag(monkeypatch):
    """Test that passing --no-color flag explicitly disables all ANSI colors."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    with patch.object(sys.stdout, "isatty", return_value=True):
        setup_color_mode(no_color=True)
        from src.core import constants

        assert constants.BLUE == ""
        assert constants.YELLOW == ""
        assert constants.OK == "✓"

    # Restore
    with patch.object(sys.stdout, "isatty", return_value=True):
        setup_color_mode(no_color=False)
        assert constants.BLUE != ""


def test_non_tty_redirection_disables_color(monkeypatch):
    """Test that output redirected to a pipe/file (non-TTY) disables ANSI colors."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    with patch.object(sys.stdout, "isatty", return_value=False):
        setup_color_mode(no_color=False)
        from src.core import constants

        assert constants.BLUE == ""
        assert constants.OK == "✓"

    # Restore for subsequent tests
    with patch.object(sys.stdout, "isatty", return_value=False):
        setup_color_mode(no_color=False)
        assert constants.BLUE == ""


def test_theme_color_sets_the_title_color(monkeypatch):
    """config.json's theme_color picks the escape THEME_TITLE resolves to."""
    monkeypatch.delenv("NO_COLOR", raising=False)
    from src.core import constants

    with patch.object(sys.stdout, "isatty", return_value=True):
        setup_color_mode(no_color=False, theme_color="purple")
        assert constants.THEME_TITLE == constants.PURPLE

        setup_color_mode(no_color=False, theme_color="cyan")
        assert constants.THEME_TITLE == constants.CYAN

        # The name is remembered, so a later call that only re-derives the
        # color mode (the --no-color path) keeps the configured title color.
        setup_color_mode(no_color=False)
        assert constants.THEME_TITLE == constants.CYAN

        # An unknown name cannot blank the title.
        setup_color_mode(no_color=False, theme_color="chartreuse")
        assert constants.THEME_TITLE == constants.PURPLE


def test_no_color_still_wins_over_the_theme_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    from src.core import constants

    with patch.object(sys.stdout, "isatty", return_value=True):
        setup_color_mode(no_color=True, theme_color="green")
        assert constants.THEME_TITLE == ""


def test_turning_colors_off_does_not_turn_terminal_control_off(monkeypatch):
    """--no-color and NO_COLOR must not blank CLEAR_SCREEN / CLEAR_LINE / ERASE_BELOW.

    They ask topo to stop colouring, not to stop driving the terminal: someone
    who exports NO_COLOR=1 still wants `topo analyze` to repaint its frame in
    place instead of smearing every frame down the scrollback. Only the absence
    of a terminal makes these meaningless, which is the next test.
    """
    from src.core import constants

    for env_no_color, flag in (("1", False), (None, True)):
        if env_no_color:
            monkeypatch.setenv("NO_COLOR", env_no_color)
        else:
            monkeypatch.delenv("NO_COLOR", raising=False)
        with patch.object(sys.stdout, "isatty", return_value=True):
            setup_color_mode(no_color=flag)

        assert constants.GREEN == ""
        assert constants.CLEAR_SCREEN == "\033[2J\033[H"
        assert constants.CLEAR_LINE == "\r\033[K"
        assert constants.ERASE_BELOW == "\033[J"


def test_non_tty_redirection_blanks_terminal_control_in_consumers(monkeypatch):
    """A pipe has no screen to clear, and the constants have to reach from-importers.

    `topo optimize > log` used to write \\033[2J plus a spinner frame's \\r\\033[K
    a dozen times a second into the log: the colors were correctly gone, the
    cursor control was not. Asserting on constants.* alone would pass even if the
    propagation pass had been left color-only, so this checks a consumer module.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    from src import optimize
    from src.core import constants

    with patch.object(sys.stdout, "isatty", return_value=True):
        setup_color_mode(no_color=False)
        assert optimize.CLEAR_SCREEN != ""

    with patch.object(sys.stdout, "isatty", return_value=False):
        setup_color_mode(no_color=False)
        assert constants.CLEAR_SCREEN == ""
        assert optimize.CLEAR_SCREEN == ""
        assert optimize.CLEAR_LINE == ""


GLYPH_NAMES = ("OK", "FAIL", "WARN", "INFO", "SKIP", "NA")


def test_every_status_glyph_loses_its_color_and_keeps_its_shape(monkeypatch):
    """The whole glyph vocabulary has to be in `_COLOR_NAMES`, and stay one column wide.

    Each glyph constant embeds its own color, so one that was added to
    constants.py but forgotten in `_COLOR_NAMES` would keep printing escapes
    under --no-color -- silently, because every other glyph on the line obeys.
    The shape has to survive too: --no-color removes the color, and the state a
    line reports must then still be readable from the glyph alone.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    from src.core import constants

    with patch.object(sys.stdout, "isatty", return_value=True):
        setup_color_mode(no_color=True)
        bare = {name: getattr(constants, name) for name in GLYPH_NAMES}
        for name, value in bare.items():
            assert "\033" not in value, name
            assert display_width(value) == 1, (name, value)
        # Six distinct states need six distinguishable glyphs.
        assert len(set(bare.values())) == len(GLYPH_NAMES)

        setup_color_mode(no_color=False)
        for name in GLYPH_NAMES:
            colored = getattr(constants, name)
            assert "\033" in colored, name
            assert colored.endswith(constants.RESET), name
            assert bare[name] in colored, name


def test_leading_marks_are_colorless(monkeypatch):
    """The three leading marks carry no color of their own, in either mode.

    Their color belongs to the caller (a heading takes THEME_TITLE, a prompt
    takes PURPLE), which is why they are absent from `_COLOR_NAMES`: propagating
    them would be a no-op, and embedding a color would fight the caller's.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    from src.core import constants

    marks = (constants.MARK_SECTION, constants.MARK_PROMPT, constants.MARK_NOTE)
    for no_color in (True, False):
        with patch.object(sys.stdout, "isatty", return_value=True):
            setup_color_mode(no_color=no_color)
        for mark in marks:
            assert "\033" not in mark
            assert display_width(mark) == 1, mark
    assert len(set(marks)) == 3


def test_no_color_flag_reaches_from_imported_consumers(monkeypatch):
    """--no-color must blank colors in modules that did `from .constants import GREEN`.

    Those modules copy the value at import time, so asserting on
    constants.GREEN alone would pass even when the flag is a no-op.
    """
    monkeypatch.delenv("NO_COLOR", raising=False)
    from src import status
    from src.ui import navigator, tui

    with patch.object(sys.stdout, "isatty", return_value=True):
        setup_color_mode(no_color=False)
        assert navigator.GREEN != ""
        assert tui.GREEN != ""
        assert status.GREEN != ""

        setup_color_mode(no_color=True)
        for module in (navigator, tui, status):
            assert module.GREEN == "", module.__name__
            assert module.RESET == "", module.__name__
        assert navigator.WHITE == ""
        # Bars must stay readable without color: distinct glyphs, not just SGR.
        assert navigator.draw_bar(0, 10) != navigator.draw_bar(100, 10)
