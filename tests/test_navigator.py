"""Characterization tests for the interactive selectors.

These drive each selector's run() loop with a scripted key sequence (terminal
I/O is mocked) so we can refactor the shared scaffolding without changing the
observable behavior.
"""

import os
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src.ui.navigator import (
    ANSI_CSI_RE,
    AnalyzeSelector,
    CleanSelector,
    ConfirmSelector,
    InteractiveMenu,
    MouseEvent,
    Navigator,
    PaginatedSelector,
    UninstallSelector,
)


@contextmanager
def _fake_raw_mode(*args, **kwargs):
    yield 0  # a dummy file descriptor


def drive(selector, keys):
    """Run selector.run() feeding it the given key sequence."""
    result, _ = drive_with_writes(selector, keys)
    return result


def drive_with_writes(selector, keys):
    """Run selector.run() feeding it the given key sequence."""
    it = iter(keys)

    def next_key(fd=None):
        return next(it)

    with (
        patch.object(Navigator, "hide_cursor"),
        patch.object(Navigator, "show_cursor"),
        patch.object(Navigator, "raw_mode", _fake_raw_mode),
        patch.object(Navigator, "get_key", side_effect=next_key),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
        patch("select.select", return_value=([], [], [])),
        patch("os.read", return_value=b""),
    ):
        return selector.run(), write.call_args_list


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


# --- InteractiveMenu ---
def test_interactive_menu_enables_mouse_tracking():
    calls = []

    @contextmanager
    def fake_raw_mode(*args, **kwargs):
        calls.append(kwargs)
        yield 0

    menu = InteractiveMenu("Main Menu", [("Clean", "Free up disk space")])

    with (
        patch.object(Navigator, "hide_cursor"),
        patch.object(Navigator, "show_cursor"),
        patch.object(Navigator, "raw_mode", fake_raw_mode),
        patch.object(Navigator, "get_key", return_value=Navigator.ESC),
        patch("sys.stdout.write"),
        patch("sys.stdout.flush"),
    ):
        assert menu.run() is None

    assert calls == [{"enable_mouse": True}]


def test_interactive_menu_mouse_wheel_scrolls_short_terminal_view():
    options = [(f"Option {index}", "menu item") for index in range(12)]
    menu = InteractiveMenu("Main Menu", options)
    keys = [
        MouseEvent("wheel_down", 1, 40, 5),
        MouseEvent("wheel_down", 1, 40, 5),
        Navigator.ESC,
    ]

    with patch(
        "src.ui.navigator.shutil.get_terminal_size",
        return_value=os.terminal_size((40, 5)),
    ):
        result, writes = drive_with_writes(menu, keys)

    output = "".join(call.args[0] for call in writes)
    assert result is None
    assert "Option 8" in output


# --- AnalyzeSelector ---
def test_analyze_space_then_enter_deletes_selected_batch():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    # New logic: Enter triggers confirmation, second Enter confirms deletion
    action, payload = drive(sel, [Navigator.SPACE, "\r", "\r"])
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


def test_analyze_render_keeps_space_between_icon_and_name():
    items = [
        {"name": "folder", "path": Path("/tmp/folder"), "size": 100, "percent": 1.0, "icon": "🗂️"},
        {
            "name": "file.txt",
            "path": Path("/tmp/file.txt"),
            "size": 50,
            "percent": 0.5,
            "icon": "📄",
        },
    ]
    sel = AnalyzeSelector("t", items, can_select=True)
    sel.selected_items.add(1)

    with (
        patch(
            "src.ui.navigator.shutil.get_terminal_size", return_value=os.terminal_size((100, 24))
        ),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        sel.render()

    output = write.call_args.args[0]
    visible_output = ANSI_CSI_RE.sub("", output)
    assert "🗂️  folder" in visible_output
    assert "📄 file.txt" in visible_output
    assert "📄  file.txt" not in visible_output


def test_analyze_render_shows_notice():
    sel = AnalyzeSelector(
        "t",
        _analyze_items(),
        can_select=True,
        notice="Preview mode: showing first 500 direct entries; folder sizes are not calculated.",
    )

    with (
        patch(
            "src.ui.navigator.shutil.get_terminal_size", return_value=os.terminal_size((100, 24))
        ),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        sel.render()

    output = write.call_args.args[0]
    visible_output = ANSI_CSI_RE.sub("", output)
    assert (
        "Preview mode: showing first 500 direct entries; folder sizes are not calculated."
        in visible_output
    )


def test_analyze_render_shows_unknown_folder_size():
    items = [
        {
            "name": "folder",
            "path": Path("/tmp/folder"),
            "size": 0,
            "percent": 0.0,
            "icon": "🗂️",
            "size_known": False,
        }
    ]
    sel = AnalyzeSelector("t", items, can_select=True, sort_mode="name")

    with (
        patch(
            "src.ui.navigator.shutil.get_terminal_size", return_value=os.terminal_size((100, 24))
        ),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        sel.render()

    visible_output = ANSI_CSI_RE.sub("", write.call_args.args[0])
    assert "folder" in visible_output
    assert "|         --" in visible_output


def test_analyze_delete_confirm_mentions_uncalculated_sizes():
    items = [
        {
            "name": "folder",
            "path": Path("/tmp/folder"),
            "size": 0,
            "percent": 0.0,
            "icon": "🗂️",
            "size_known": False,
        },
        {
            "name": "file.txt",
            "path": Path("/tmp/file.txt"),
            "size": 4,
            "percent": 100.0,
            "icon": "📄",
            "size_known": True,
        },
    ]
    sel = AnalyzeSelector("t", items, can_select=True, sort_mode="name")

    with patch.object(Navigator, "play_click"):
        drive(
            sel,
            [Navigator.SPACE, Navigator.DOWN, Navigator.SPACE, "\r", Navigator.ESC, Navigator.ESC],
        )

    assert "4 B known, 1 uncalculated" in sel.confirm_text


def test_analyze_name_sort_keeps_directories_first_when_reversed():
    items = [
        {"name": "a-file", "path": Path("/tmp/a-file"), "size": 1, "percent": 1.0, "sort_group": 1},
        {"name": "b-dir", "path": Path("/tmp/b-dir"), "size": 0, "percent": 0.0, "sort_group": 0},
        {"name": "a-dir", "path": Path("/tmp/a-dir"), "size": 0, "percent": 0.0, "sort_group": 0},
        {"name": "z-file", "path": Path("/tmp/z-file"), "size": 1, "percent": 1.0, "sort_group": 1},
    ]
    sel = AnalyzeSelector("t", items, can_select=True, sort_mode="name")

    assert [item["name"] for item in sel.items] == ["a-dir", "b-dir", "a-file", "z-file"]

    sel.sort_reverse = True
    sel._sort_items()

    assert [item["name"] for item in sel.items] == ["b-dir", "a-dir", "z-file", "a-file"]


# --- UninstallSelector ---
def test_uninstall_space_then_enter_returns_indices():
    sel = UninstallSelector("t", _uninstall_items())
    result = drive(sel, [Navigator.SPACE, "\r"])
    assert result == [0]


def test_uninstall_defaults_to_install_time_sort():
    items = [
        {
            "id": "old-large",
            "name": "old-large",
            "size_bytes": 999_000,
            "size_str": "999 KB",
            "install_time": 100,
        },
        {
            "id": "new-small",
            "name": "new-small",
            "size_bytes": 1_000,
            "size_str": "1 KB",
            "install_time": 200,
        },
    ]

    sel = UninstallSelector("t", items)

    assert sel.sort_key == "install_time"
    assert [item["id"] for item in sel.items] == ["new-small", "old-large"]


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


def test_short_terminal_render_draws_right_edge_scrollbar():
    items = [{"name": f"task{i}", "size": 100 + i, "desc": "cleanup target"} for i in range(8)]
    selector = CleanSelector("t", items)
    selector.selected_index = 7

    with (
        patch("src.ui.navigator.shutil.get_terminal_size", return_value=os.terminal_size((40, 5))),
        patch("src.ui.navigator.get_show_scrollbar", return_value=True),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        selector.render()

    output = write.call_args.args[0]
    assert "\033[1;40H" in output
    assert "\033[5;40H" in output
    assert "\033[5;40H\033[J" in output
    assert "▐" in output
    assert "┃" not in output
    assert selector._frame_state.scrollable is True
    assert selector._frame_state.scrollbar_visible is True
    assert "\033[1;37m" not in output
    assert "task7" in output
    assert "task0" not in output


def test_short_terminal_render_can_hide_right_edge_scrollbar():
    items = [{"name": f"task{i}", "size": 100 + i, "desc": "cleanup target"} for i in range(8)]
    selector = CleanSelector("t", items)
    selector.selected_index = 7

    with (
        patch("src.ui.navigator.shutil.get_terminal_size", return_value=os.terminal_size((40, 5))),
        patch("src.ui.navigator.get_show_scrollbar", return_value=False),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        selector.render()

    output = write.call_args.args[0]
    assert "▐" not in output
    assert "┃" not in output
    assert selector._frame_state.scrollable is True
    assert selector._frame_state.scrollbar_visible is False
    assert "task7" in output


def test_sgr_mouse_drag_sequence_is_parsed():
    assert Navigator._parse_sgr_mouse("\x1b[<0;40;2M") == MouseEvent("press", 0, 40, 2)
    assert Navigator._parse_sgr_mouse("\x1b[<32;40;5M") == MouseEvent("drag", 0, 40, 5)
    assert Navigator._parse_sgr_mouse("\x1b[<0;40;5m") == MouseEvent("release", 0, 40, 5)


def test_scrollbar_drag_scrolls_short_terminal_view():
    items = [{"name": f"task{i}", "size": 100 + i, "desc": "cleanup target"} for i in range(10)]
    selector = CleanSelector("t", items)
    keys = [
        MouseEvent("press", 0, 40, 1),
        MouseEvent("drag", 0, 40, 5),
        MouseEvent("release", 0, 40, 5),
        Navigator.ESC,
    ]

    with (
        patch(
            "src.ui.navigator.shutil.get_terminal_size",
            return_value=os.terminal_size((40, 5)),
        ),
        patch("src.ui.navigator.get_show_scrollbar", return_value=True),
    ):
        result, writes = drive_with_writes(selector, keys)

    output = "".join(call.args[0] for call in writes)
    assert result == []
    assert "task9" in output


def test_mouse_wheel_still_scrolls_when_right_edge_scrollbar_is_hidden():
    items = [{"name": f"task{i}", "size": 100 + i, "desc": "cleanup target"} for i in range(10)]
    selector = CleanSelector("t", items)
    keys = [
        MouseEvent("wheel_down", 1, 20, 3),
        MouseEvent("wheel_down", 1, 20, 3),
        MouseEvent("wheel_down", 1, 20, 3),
        Navigator.ESC,
    ]

    with (
        patch(
            "src.ui.navigator.shutil.get_terminal_size",
            return_value=os.terminal_size((40, 5)),
        ),
        patch("src.ui.navigator.get_show_scrollbar", return_value=False),
    ):
        result, writes = drive_with_writes(selector, keys)

    output = "".join(call.args[0] for call in writes)
    assert result == []
    assert "▐" not in output
    assert "task9" in output


def test_clean_selector_empty_items_returns_without_error():
    # Empty items must not raise ZeroDivisionError on cursor movement; run() exits.
    assert CleanSelector("t", []).run() == []
