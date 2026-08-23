import sys
from unittest.mock import patch

import pytest

from src.core.constants import setup_color_mode


@pytest.fixture(autouse=True)
def _restore_real_color_mode():
    """Re-derive the color mode from the real environment after each test.

    setup_color_mode() now rebinds the color names inside every consumer module
    (that is the point of the --no-color fix), so a test that leaves colors
    forced on would leak escape codes into unrelated tests.
    """
    yield
    setup_color_mode(no_color=False)


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
