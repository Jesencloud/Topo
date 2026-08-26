from unittest.mock import patch

from src.ui import tui


def test_main_menu_returns_action_id_for_selected_item():
    with patch("src.ui.tui.InteractiveMenu.run", return_value=0):
        assert tui.main_menu() == tui.CLEAN_ACTION


def test_main_menu_returns_quit_action_when_cancelled():
    with patch("src.ui.tui.InteractiveMenu.run", return_value=None):
        assert tui.main_menu() == tui.QUIT_ACTION


def test_render_banner_returns_text_with_version():
    banner = tui.render_banner()

    assert "is digging deeper" in banner
    assert f"v{tui.TOPO_VERSION}" in banner


def test_render_banner_is_stable_across_redraws():
    """The status dot used to cycle color on every call.

    render_banner() is called once per full redraw, i.e. once per keystroke, so
    the cycling tracked typing speed rather than any program state.
    """
    assert tui.render_banner() == tui.render_banner() == tui.render_banner()


def test_render_banner_emits_no_escapes_when_colors_are_off():
    # Colors are already blanked under pytest (non-TTY); the dot's palette used to
    # be a module-level list of inline SGR literals that _propagate_constants()
    # could not reach, so it stayed colored under --no-color.
    assert "\033" not in tui.render_banner()
