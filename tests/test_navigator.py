"""Characterization tests for the interactive selectors.

These drive each selector's run() loop with a scripted key sequence (terminal
I/O is mocked) so we can refactor the shared scaffolding without changing the
observable behavior.
"""

import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from src.core.text import display_width
from src.ui.navigator import (
    ANSI_CSI_RE,
    AnalyzeSelector,
    ConfirmSelector,
    InteractiveMenu,
    MouseEvent,
    Navigator,
    UninstallPreviewSelector,
    UninstallSelector,
    draw_bar,
    format_percent,
    icon_gap,
    pad_and_truncate,
)
from src.ui.screens.uninstall import SCREEN_TITLE


@contextmanager
def _fake_raw_mode(*args, **kwargs):
    yield 0  # a dummy file descriptor


def test_raw_mode_registers_and_restores_terminal_state():
    calls = []

    with (
        patch("sys.stdin.fileno", return_value=42),
        patch("src.ui.navigator.termios.tcgetattr", return_value="old-settings"),
        patch("src.ui.navigator.tty.setcbreak") as setcbreak,
        patch("src.ui.navigator.terminal_state.remember_raw_state") as remember_raw_state,
        patch("src.ui.navigator.terminal_state.restore_raw_state") as restore_raw_state,
        patch("src.ui.navigator.terminal_state.disable_mouse_tracking") as disable_mouse_tracking,
        patch("src.ui.navigator.terminal_state.enable_mouse_tracking") as enable_mouse_tracking,
        Navigator.raw_mode(enable_mouse=True) as fd,
    ):
        calls.append(fd)

    assert calls == [42]
    remember_raw_state.assert_called_once_with(42, "old-settings")
    setcbreak.assert_called_once_with(42)
    enable_mouse_tracking.assert_called_once_with()
    assert disable_mouse_tracking.call_count == 2
    restore_raw_state.assert_called_once_with(42, "old-settings")


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


def render_buffer(selector, width=100):
    """Render *selector* and return the frame buffer it handed to the writer.

    Captures the buffer rather than what reaches stdout: the frame writer
    positions the cursor per row instead of emitting newlines, so splitlines()
    over its output would see one very long line.
    """
    frames = []
    with (
        patch(
            "src.ui.navigator.shutil.get_terminal_size",
            return_value=os.terminal_size((width, 24)),
        ),
        patch(
            "src.ui.navigator._render_scrollable_frame",
            side_effect=lambda _owner, parts, _focus=None: frames.append(parts),
        ),
    ):
        selector.render()
    return "".join(frames[0])


def render_lines(selector, width=100):
    """render_buffer with the escape sequences stripped, split into lines."""
    return ANSI_CSI_RE.sub("", render_buffer(selector, width)).splitlines()


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


def _confirm_button_line(selected_index):
    selector = ConfirmSelector("Delete 3 items?")
    selector.selected_index = selected_index

    with patch("sys.stdout.write") as write, patch("sys.stdout.flush"):
        selector.render()

    rendered = "".join(call.args[0] for call in write.call_args_list)
    visible = ANSI_CSI_RE.sub("", rendered)
    return next(line for line in visible.split("\n") if "Yes" in line and "No" in line)


def test_confirm_selection_is_visible_without_color():
    """The armed button must differ in *characters*, not only in background color.

    Colors are blanked under pytest (non-TTY), which is the same state a user gets
    from --no-color, NO_COLOR or a piped run. The dialog gates deletions, so
    "which button is armed" cannot be a color-only distinction: it used to render
    as a one-space shift, indistinguishable in practice.
    """
    yes_armed = _confirm_button_line(0)
    no_armed = _confirm_button_line(1)

    assert yes_armed.strip() != no_armed.strip()
    # Same total width either way, so toggling never nudges the row sideways.
    assert display_width(yes_armed) == display_width(no_armed)
    assert "▸ Yes ◂" in yes_armed and "▸ No ◂" not in yes_armed
    assert "▸ No ◂" in no_armed and "▸ Yes ◂" not in no_armed


# --- pad_and_truncate / icon_gap ---
def test_pad_and_truncate_never_exceeds_requested_width():
    # Widths under len("...") used to return a bare "..." and overflow the column,
    # breaking every row drawn after it.
    for width in range(0, 8):
        for text in ("", "a", "abcdefghij", "中文测试字符"):
            assert display_width(pad_and_truncate(text, width)) == width, (text, width)


def test_pad_and_truncate_pads_and_truncates():
    assert pad_and_truncate("abc", 10) == "abc       "
    assert pad_and_truncate("中文测试", 10) == "中文测试  "
    assert pad_and_truncate("verylongfilename", 12) == "verylongf..."


def test_icon_gap_aligns_names_after_icons_of_different_widths():
    narrow = "\U0001f5c2️"  # card index dividers + VS16: one cell
    wide = "\U0001f4c4"  # page facing up: two cells
    assert display_width(narrow + icon_gap(narrow)) == display_width(wide + icon_gap(wide))
    assert icon_gap(narrow) == "  "
    assert icon_gap(wide) == " "


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


def test_interactive_menu_mouse_wheel_moves_cursor():
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
    assert menu.selected_index == 2
    assert "Option 2" in output


def test_interactive_menu_uses_returned_banner_text():
    menu = InteractiveMenu(
        "Main Menu",
        [("Clean", "Free up disk space")],
        show_banner=lambda: "BANNER\n",
    )

    result, writes = drive_with_writes(menu, [Navigator.ESC])

    output = "".join(call.args[0] for call in writes)
    assert result is None
    assert "BANNER" in output


def test_interactive_menu_digit_selects_and_activates():
    # Labels are numbered ("1. Clean"), so pressing the digit must pick that row.
    options = [(f"{i + 1}. Option", "menu item") for i in range(5)]
    menu = InteractiveMenu("Main Menu", options)

    assert drive(menu, ["3"]) == 2
    assert menu.selected_index == 2


def test_interactive_menu_digit_out_of_range_is_ignored():
    menu = InteractiveMenu("Main Menu", [("1. Clean", "d"), ("2. Status", "d")])

    assert drive(menu, ["9", Navigator.ESC]) is None
    assert menu.selected_index == 0


def test_interactive_menu_zero_is_not_a_selection():
    menu = InteractiveMenu("Main Menu", [("1. Clean", "d")])

    assert drive(menu, ["0", Navigator.ESC]) is None


# --- draw_bar ---
def test_draw_bar_levels_differ_without_color():
    # Colors are blanked under pytest (non-TTY), so this asserts the glyphs alone
    # carry the fill level: without that fallback every bar would look full.
    bars = [draw_bar(percent, width=10) for percent in (0, 25, 50, 75, 100)]
    assert len(set(bars)) == len(bars)
    assert bars[0] == "─" * 10
    assert bars[-1] == "▬" * 10


def test_draw_bar_keeps_the_original_glyph_for_both_segments_when_colored():
    with patch.multiple(
        "src.core.render", RESET="\033[0m", WHITE="\033[38;5;244m", GREEN="\033[1;32m"
    ):
        bar = draw_bar(50, width=10)
    assert ANSI_CSI_RE.sub("", bar) == "▬" * 10


def test_draw_bar_keeps_requested_width():
    for percent in (-5, 0, 1, 37, 99.9, 100, 250):
        assert len(draw_bar(percent, width=20)) == 20, percent


def test_draw_bar_zero_width_is_empty():
    assert draw_bar(50, width=0) == ""


def test_draw_bar_zero_percent_honors_force_color():
    # A battery at 0% passes force_color=RED; falling back to the neutral gray
    # track would drop the warning exactly when it matters most.
    # Only RESET and WHITE are read on this path (RESET picks the glyph and
    # terminates the run, WHITE is the default track color), so patching a RED
    # that draw_bar never looks at would just be noise.
    with patch.multiple("src.core.render", RESET="\033[0m", WHITE="\033[38;5;244m"):
        forced = draw_bar(0, width=10, force_color="\033[1;31m")
        default = draw_bar(0, width=10)

    assert forced.startswith("\033[1;31m")
    assert default.startswith("\033[38;5;244m")
    assert ANSI_CSI_RE.sub("", forced) == ANSI_CSI_RE.sub("", default)


# --- format_percent ---
def test_format_percent_marks_only_the_shares_that_round_away():
    # These all print as "0.0" at one decimal, which reads as "nothing here" for
    # an entry that does occupy space, so they must read as a bound instead.
    for percent in (1e-9, 0.004, 0.04, 0.0449, 0.049999):
        assert format_percent(percent) == " <0.1%", percent


def test_format_percent_keeps_a_share_that_rounds_up_to_a_tenth():
    # From 0.05 upward the rounded number is "0.1", which is a real reading of
    # the share, so it stays as it was before the bound was introduced.
    for percent in (0.05, 0.06, 0.09, 0.0999, 0.1):
        assert format_percent(percent) == "  0.1%", percent


def test_format_percent_keeps_the_number_from_zero_upward():
    # Exactly zero is the one share "0.0%" describes truthfully, so it stays.
    assert format_percent(0) == "  0.0%"
    assert format_percent(12.34) == " 12.3%"
    assert format_percent(100) == "100.0%"


def test_format_percent_holds_its_column_across_the_boundary():
    # The bar/percent/name row is built by concatenation, so a field that changes
    # width would shift every column to its right on just the small entries.
    widths = {display_width(format_percent(percent)) for percent in (0, 0.04, 0.09, 7.5, 100)}
    assert widths == {6}


# --- AnalyzeSelector ---
def test_analyze_space_then_enter_deletes_selected_batch():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    # New logic: Enter triggers confirmation, second Enter confirms deletion
    action, payload = drive(sel, [Navigator.SPACE, "\r", "\r"])
    assert action == "DELETE_BATCH"
    assert payload == [0]


def test_analyze_del_no_longer_deletes_selected_batch():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    action, _ = drive(sel, [Navigator.SPACE, "\x7f", Navigator.ESC])
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


def test_analyze_zero_selects_nothing_and_ten_is_typed_in_full():
    """ "0" used to mean "row 10", so the key pressed disagreed with the screen.

    No row is numbered 0, so it must match nothing; row 10 is reached by typing
    "10". The old shortcut also only lined up on page 1 -- once rows are numbered
    16-30 there is no 10 to jump to -- so it could not survive the move to
    accumulating numbers across pages.
    """
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    action, _ = drive(sel, ["0", Navigator.ESC])
    assert action == "QUIT"
    assert sel.selected_items == set()

    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    drive(sel, ["10", Navigator.ESC])
    assert sel.selected_items == {9}


def test_analyze_down_moves_cursor():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    drive(sel, [Navigator.DOWN, Navigator.DOWN, Navigator.ESC])
    assert sel.selected_index == 2


def test_analyze_mouse_wheel_moves_cursor():
    sel = AnalyzeSelector("t", _analyze_items(), can_select=True)
    drive(
        sel, [MouseEvent("wheel_down", 1, 20, 5), MouseEvent("wheel_up", 1, 20, 5), Navigator.ESC]
    )
    assert sel.selected_index == 0


def test_analyze_mouse_wheel_moves_cursor_for_three_item_view():
    sel = AnalyzeSelector("t", _analyze_items(3), can_select=False)
    drive(sel, [MouseEvent("wheel_down", 1, 20, 5), Navigator.ESC])
    assert sel.selected_index == 1


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


def test_analyze_render_keeps_columns_fixed_between_directory_levels():
    item = {
        "name": "folder",
        "path": Path("/tmp/folder"),
        "size": 100,
        "percent": 12.5,
        "icon": "📄",
    }

    def render_row(can_select):
        selector = AnalyzeSelector("t", [item.copy()], can_select=can_select)
        with (
            patch(
                "src.ui.navigator.shutil.get_terminal_size",
                return_value=os.terminal_size((100, 24)),
            ),
            patch("sys.stdout.write") as write,
            patch("sys.stdout.flush"),
        ):
            selector.render()

        rows = [
            ANSI_CSI_RE.sub("", row) for row in re.split(r"\x1b\[\d+;1H", write.call_args.args[0])
        ]
        return next(row for row in rows if "folder" in row)

    root_row = render_row(can_select=False)
    child_row = render_row(can_select=True)

    for token in ("12.5%", "📄", "folder"):
        assert root_row.index(token) == child_row.index(token)


def test_analyze_render_shows_a_bound_for_a_tiny_share():
    """A file too small to reach 0.1% must not be presented as 0.0% of the disk.

    Rendering both rows in one pass also pins the alignment: the name column can
    only line up if the percent field kept its width for the small entry.
    """
    items = [
        {"name": "big", "path": Path("/tmp/big"), "size": 10**9, "percent": 99.5, "icon": "🗂️"},
        {"name": "tiny", "path": Path("/tmp/tiny"), "size": 512, "percent": 0.00005, "icon": "📄"},
    ]
    sel = AnalyzeSelector("t", items, can_select=True)

    with (
        patch(
            "src.ui.navigator.shutil.get_terminal_size", return_value=os.terminal_size((100, 24))
        ),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        sel.render()

    # render() places each row with a cursor-move escape instead of a newline.
    rows = [ANSI_CSI_RE.sub("", row) for row in re.split(r"\x1b\[\d+;1H", write.call_args.args[0])]
    big_row = next(row for row in rows if "big" in row)
    tiny_row = next(row for row in rows if "tiny" in row)
    assert "<0.1%" in tiny_row
    assert "0.0%" not in tiny_row
    assert big_row.index("🗂️") == tiny_row.index("📄")


def test_analyze_render_aligns_a_row_whose_size_is_unknown():
    """An unknown size must hold the percent column, not shift the row left.

    Fast explore leaves every directory's size unknown and sorts directories
    ahead of the files, so a one-column difference here does not read as a stray
    row: it splits the frame into two blocks that do not line up.
    """
    items = [
        {"name": "file", "path": Path("/tmp/file"), "size": 512, "percent": 62.4, "icon": "📄"},
        {
            "name": "dir",
            "path": Path("/tmp/dir"),
            "size": 0,
            "percent": 0.0,
            "icon": "📄",
            "size_known": False,
        },
    ]
    sel = AnalyzeSelector("t", items, can_select=True)

    with (
        patch(
            "src.ui.navigator.shutil.get_terminal_size", return_value=os.terminal_size((100, 24))
        ),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        sel.render()

    rows = [ANSI_CSI_RE.sub("", row) for row in re.split(r"\x1b\[\d+;1H", write.call_args.args[0])]
    known_row = next(row for row in rows if "file" in row)
    unknown_row = next(row for row in rows if "dir" in row)
    assert "--" in unknown_row
    # Both rows carry the same icon, so the columns are directly comparable: the
    # icon, the name behind it and the size field past the '|' all follow the
    # percent field by concatenation.
    for column in ("📄", "|"):
        assert display_width(known_row[: known_row.index(column)]) == display_width(
            unknown_row[: unknown_row.index(column)]
        ), column


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


def test_analyze_marks_only_that_the_list_continues():
    """A page with rows after it ends in "..."; the last page ends bare.

    This replaced a "↑ More items above | ↓ More items below" pair. Nothing marks
    the rows above any more: landing on a later page is something the user just
    did, and the footer's page counter already says where they are.
    """

    def has_continuation_marker(selector, page=0):
        selector.current_page = page
        selector.selected_index = page * selector.page_size
        lines = render_lines(selector)
        # A whole line of its own -- pad_and_truncate also emits "..." inside a
        # clipped filename, so a substring test would answer the wrong question.
        return any(line.strip() == "..." for line in lines)

    # 31 items at 15 a page: two full pages, then a single trailing row.
    paged = AnalyzeSelector("t", _analyze_items(31), can_select=True)
    assert has_continuation_marker(paged, 0)
    assert has_continuation_marker(paged, 1)
    assert not has_continuation_marker(paged, 2)

    # A list that fits on one page is never marked as continuing.
    assert not has_continuation_marker(AnalyzeSelector("t", _analyze_items(5), can_select=True))


def test_analyze_page_numbers_accumulate_but_digits_select_current_page():
    selector = AnalyzeSelector("t", _analyze_items(30), can_select=True)
    action, _ = drive(selector, [Navigator.PGDN, "15", "16", "30", "31", Navigator.ESC])

    assert action == "QUIT"
    assert selector.selected_items == {15, 29}

    selector.current_page = 1
    selector.selected_index = 15
    with (
        patch(
            "src.ui.navigator.shutil.get_terminal_size",
            return_value=os.terminal_size((100, 24)),
        ),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        selector.render()
    visible = ANSI_CSI_RE.sub("", write.call_args.args[0])
    assert "16." in visible


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

    with patch("src.ui.navigator.play_click"):
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
def test_uninstall_hint_uses_gray():
    selector = UninstallSelector("t", _uninstall_items()[:2])
    # The colours have to be stand-ins during the render itself, so the patch
    # wraps the call rather than living inside the helper.
    with patch.multiple("src.ui.navigator", GREEN="<GREEN>", GRAY="<GRAY>", RESET="<RESET>"):
        output = render_buffer(selector, 200)

    footer = next(line for line in output.splitlines() if "Page 1/" in line)
    for key in ("↑↓←→", "PgUp/PgDn", "A", "N", "S", "T"):
        assert key in footer
    assert "<GREEN>" not in footer
    assert footer.lstrip().startswith("<GRAY>")

    # The selection hint moved to the header line, which has to stay gray too.
    header_hint = next(line for line in output.splitlines() if "Select apps to uninstall" in line)
    assert "<GREEN>" not in header_hint
    assert header_hint.lstrip().startswith("<GRAY>")


def test_uninstall_rows_reflow_as_terminal_narrows():
    item = {
        "id": "responsive",
        "name": "A very long application name that needs truncation",
        "size_bytes": 2048,
        "size_str": "2.0 KiB",
        "install_time": time.time() - 7200,
    }
    selector = UninstallSelector("t", [item])

    def render_at(width):
        lines = render_lines(selector, width)
        return next(line for line in lines if "A very" in line)

    wide = render_at(80)
    medium = render_at(50)
    narrow = render_at(35)

    assert "2.0 KiB" in wide and "2h ago" in wide
    assert "2.0 KiB" in medium and "2h ago" not in medium
    assert "2.0 KiB" not in narrow and "2h ago" not in narrow
    assert display_width(wide) <= 80
    assert display_width(medium) <= 50
    assert display_width(narrow) <= 35


def test_uninstall_footer_stays_on_one_rendered_line_at_all_widths():
    selector = UninstallSelector("t", _uninstall_items())
    for width in (35, 80, 120):
        lines = render_lines(selector, width)
        footer_lines = [line for line in lines if "Page 1/" in line]

        assert len(footer_lines) == 1
        footer = footer_lines[0]
        for hint in (
            "↑↓←→",
            "PgUp/PgDn",
            "A: All",
            "N: Name",
            "S: Size",
            "T: Time",
        ):
            assert hint in footer
        # How to select is explained in the header line, not crammed in here.
        assert "Select" not in footer


def test_uninstall_header_explains_how_to_select():
    """The selection hint sits under the title, matching Analyze.

    It used to be a "Space: Select" fragment at the end of the footer, which ran
    the footer past 80 columns -- so the one hint telling you the digit shortcut
    exists was the first thing truncated away on a standard terminal.
    """
    lines = render_lines(UninstallSelector(SCREEN_TITLE, _uninstall_items()))
    title = next(i for i, line in enumerate(lines) if SCREEN_TITLE in line)
    hint = next(
        i for i, line in enumerate(lines) if line.strip().startswith("Select apps to uninstall")
    )

    assert hint > title
    # Compared stripped: the leading indent is a layout detail being tuned.
    assert lines[hint].strip() == (
        "Select apps to uninstall (Enter item numbers from this page, or press Space to select):"
    )
    # The title names the screen so the sentence under it can carry the
    # instruction; a title phrased as one would say "select" twice in a row.
    assert "select" not in SCREEN_TITLE.lower()
    assert "0/20 selected" in lines[title]
    for line in lines:
        assert display_width(line) <= 100


def test_uninstall_selector_renders_the_title_it_was_given():
    """The title is a constructor argument, not a string baked into render().

    It was hardcoded while __init__ still stored the argument, so the screen and
    its scan spinner each carried their own copy and could drift apart -- the
    title would jump the moment the scan handed off to the list.
    """
    lines = render_lines(UninstallSelector("Some Other Screen", _uninstall_items()))
    assert any("Some Other Screen" in line for line in lines)


def test_uninstall_empty_list_does_not_offer_a_selection_hint():
    """Nothing to number, so telling the user to type a row number would be noise."""
    lines = render_lines(UninstallSelector("t", []))
    assert any("No applications found" in line for line in lines)
    assert not any("Select apps to uninstall" in line for line in lines)


def test_uninstall_space_then_enter_returns_indices():
    sel = UninstallSelector("t", _uninstall_items())
    result = drive(sel, [Navigator.SPACE, "\r"])
    assert result == [0]


def test_uninstall_page_numbers_accumulate_but_digits_select_current_page():
    """Rows are numbered across the list, matching Analyze.

    Page 2 shows 16-30, so those are the numbers that select there; a number
    belonging to another page is ignored rather than reaching a row the user
    cannot see. The per-page scheme this replaced took "20" on page 1 as page
    offset 20 and toggled row 20 -- on page 2.
    """
    selector = UninstallSelector("t", _uninstall_items(30))
    drive(selector, [Navigator.PGDN, "15", "16", "30", "31", Navigator.ESC])

    # 15 and 31 are off this page; 16 and 30 are its first and last rows.
    assert selector.selected_items == {"app15", "app29"}

    selector = UninstallSelector("t", _uninstall_items(30))
    drive(selector, ["20", Navigator.ESC])
    assert selector.selected_items == set()


def test_uninstall_zero_selects_nothing():
    """No row is numbered 0, so it matches nothing -- row 10 is typed in full."""
    selector = UninstallSelector("t", _uninstall_items(30))
    drive(selector, ["0", Navigator.ESC])
    assert selector.selected_items == set()

    selector = UninstallSelector("t", _uninstall_items(30))
    drive(selector, ["10", Navigator.ESC])
    assert selector.selected_items == {"app9"}


def test_uninstall_number_column_widens_for_three_digit_rows():
    """A 100+ row list must not push the name column right by a cell.

    Numbers now run across the whole list, so the column is sized from the
    largest one. Short lists keep the two-cell column they always had.
    """

    def first_row(count):
        lines = render_lines(UninstallSelector("t", _uninstall_items(count)))
        return next(line for line in lines if "app0" in line)

    # The cursor sits on the first row, so the prefix carries it.
    assert first_row(20).startswith("▶ ○  1. app0")
    assert first_row(120).startswith("▶ ○   1. app0")
    # The extra number cell comes out of the name budget, not the line width.
    assert display_width(first_row(120)) <= 100


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


def test_uninstall_mouse_wheel_moves_cursor():
    sel = UninstallSelector("t", _uninstall_items())
    drive(sel, [MouseEvent("wheel_down", 1, 20, 5), Navigator.ESC])
    assert sel.selected_index == 1


def test_uninstall_delete_key_does_not_confirm_selected_app():
    sel = UninstallSelector("t", _uninstall_items())
    result = drive(sel, [Navigator.SPACE, "\x1b[3~", Navigator.ESC])
    assert result == []
    assert sel.selected_items == {"app0"}


def test_uninstall_esc_returns_empty():
    assert drive(UninstallSelector("t", _uninstall_items()), [Navigator.ESC]) == []


def test_uninstall_preview_enter_confirms_and_renders_targets(test_env):
    app = {
        "id": "test",
        "name": "Test App",
        "size_bytes": 2048,
        "size_str": "2.0 KiB",
        "install_time": 0,
    }
    targets = [(app, [test_env / ".test-app", Path("/opt/test-app")], True)]
    selector = UninstallPreviewSelector(targets)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch(
            "src.ui.navigator.shutil.get_terminal_size",
            return_value=os.terminal_size((100, 30)),
        ),
    ):
        result, writes = drive_with_writes(selector, ["\r"])

    output = "".join(call.args[0] for call in writes)
    visible_output = ANSI_CSI_RE.sub("", output)
    assert result is True
    assert "Uninstallation Preview" in visible_output
    assert "Test App" in visible_output
    assert "[Running]" in visible_output
    assert "~/.test-app" in visible_output
    assert "/opt/test-app" in visible_output
    assert "Remove 1 application, 2.0 KiB" in visible_output


def test_uninstall_preview_space_cancels(test_env):
    app = {"name": "Test App", "size_bytes": 2048}
    selector = UninstallPreviewSelector([(app, [], False)])

    with patch("pathlib.Path.home", return_value=test_env):
        assert drive(selector, [Navigator.SPACE]) is False


def test_sgr_mouse_drag_sequence_is_parsed():
    assert Navigator._parse_sgr_mouse("\x1b[<0;40;2M") == MouseEvent("press", 0, 40, 2)
    assert Navigator._parse_sgr_mouse("\x1b[<32;40;5M") == MouseEvent("drag", 0, 40, 5)
    assert Navigator._parse_sgr_mouse("\x1b[<0;40;5m") == MouseEvent("release", 0, 40, 5)
