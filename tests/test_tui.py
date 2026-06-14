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
