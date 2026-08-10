import sys
from unittest.mock import patch

from src.core.constants import setup_color_mode


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
        assert constants.FAIL == "✗"

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
    with patch.object(sys.stdout, "isatty", return_value=True):
        setup_color_mode(no_color=False)
