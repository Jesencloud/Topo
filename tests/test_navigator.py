"""Characterization tests for the interactive selectors.

These drive each selector's run() loop with a scripted key sequence (terminal
I/O is mocked) so we can refactor the shared scaffolding without changing the
observable behavior.
"""

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src.ui.navigator import (
    AnalyzeSelector,
    ConfirmSelector,
    Navigator,
    PaginatedSelector,
    UninstallSelector,
)


@contextmanager
def _fake_raw_mode():
    yield 0  # a dummy file descriptor


def drive(selector, keys):
    """Run selector.run() feeding it the given key sequence."""
    it = iter(keys)

    def next_key(fd=None):
        return next(it)

    with (
        patch.object(Navigator, "hide_cursor"),
        patch.object(Navigator, "show_cursor"),
        patch.object(Navigator, "raw_mode", _fake_raw_mode),
        patch.object(Navigator, "get_key", side_effect=next_key),
        patch("sys.stdout.write"),
        patch("sys.stdout.flush"),
        patch("select.select", return_value=([], [], [])),
        patch("os.read", return_value=b""),
    ):
        return selector.run()


def _analyze_items(n=20):
    return [
        {"name": f"item{i}", "path": Path("/tmp"), "size": (n - i) * 100, "percent": 1.0}
        for i in range(n)
    ]


def _uninstall_items(n=20):
    return [
        {
            "id": f"app{i}",
            "name": f"app{i}",
            "size_bytes": (n - i) * 1000,
            "size_str": "1.0 KB",
            "install_time": 0,
        }
        for i in range(n)
    ]


# --- ConfirmSelector ---
def test_confirm_yes_key():
    assert drive(ConfirmSelector("ok?"), ["y"]) is True


def test_confirm_no_key():
    assert drive(ConfirmSelector("ok?"), ["n"]) is False


def test_confirm_left_then_enter_selects_yes():
    # starts on "No" (index 1); LEFT toggles to "Yes" (index 0); ENTER confirms
    assert drive(ConfirmSelector("ok?"), [Navigator.LEFT, "\r"]) is True


def test_confirm_esc_is_false():
    assert drive(ConfirmSelector("ok?"), [Navigator.ESC]) is False


# --- AnalyzeSelector ---
def test_analyze_space_then_enter_deletes_selected_batch():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    action, payload = drive(sel, [Navigator.SPACE, "\r"])
    assert action == "DELETE_BATCH"
    assert payload == [0]


def test_analyze_del_no_longer_deletes_selected_batch():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    action, _ = drive(sel, [Navigator.SPACE, Navigator.DEL, Navigator.ESC])
    assert action == "QUIT"
    assert sel.selected_items == {0}


def test_analyze_delete_sequence_does_not_delete_selected_batch():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    action, _ = drive(sel, [Navigator.SPACE, "\x1b[3~", Navigator.ESC])
    assert action == "QUIT"
    assert sel.selected_items == {0}


def test_analyze_quit_keeps_selection():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    action, _ = drive(sel, [Navigator.SPACE, Navigator.ESC])
    assert action == "QUIT"
    assert sel.selected_items == {0}


def test_analyze_number_toggles_index():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    # "3" toggles the 3rd row on the current page (index 2), then quit
    action, _ = drive(sel, ["3", Navigator.ESC])
    assert action == "QUIT"
    assert sel.selected_items == {2}


def test_analyze_down_moves_cursor():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    drive(sel, [Navigator.DOWN, Navigator.DOWN, Navigator.ESC])
    assert sel.selected_index == 2


def test_analyze_enter_drills_down():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    action, idx = drive(sel, ["\r"])
    assert action == "DRILL_DOWN"
    assert idx == 0


def test_analyze_empty_view_waits_for_back():
    sel = AnalyzeSelector("t", [], can_select=True)
    action, idx = drive(sel, [Navigator.LEFT])
    assert action == "BACK"
    assert idx is None


# --- UninstallSelector ---
def test_uninstall_space_then_enter_returns_indices():
    sel = UninstallSelector("t", _uninstall_items())
    # selection is sorted by size desc; index 0 is the largest
    result = drive(sel, [Navigator.SPACE, "\r"])
    assert result == [0]


def test_uninstall_enter_without_selection_does_not_confirm_hovered_app():
    sel = UninstallSelector("t", _uninstall_items())
    result = drive(sel, ["\r", Navigator.ESC])
    assert result == []


def test_uninstall_delete_key_does_not_confirm_selected_app():
    sel = UninstallSelector("t", _uninstall_items())
    result = drive(sel, [Navigator.SPACE, "\x1b[3~", Navigator.ESC])
    assert result == []
    assert sel.selected_ids == {"app0"}


def test_uninstall_esc_returns_empty():
    assert drive(UninstallSelector("t", _uninstall_items()), [Navigator.ESC]) == []


# --- PaginatedSelector ---
def test_paginated_manage_paths():
    items = [{"project": f"p{i}", "path": Path("/tmp"), "size": 100} for i in range(5)]
    assert drive(PaginatedSelector("t", items), ["s"]) == "MANAGE_PATHS"


def test_paginated_enter_defaults_to_hover():
    items = [{"project": f"p{i}", "path": Path("/tmp"), "size": 100} for i in range(5)]
    assert drive(PaginatedSelector("t", items), ["\r"]) == [0]
