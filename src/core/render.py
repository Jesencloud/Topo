"""Number formatting: byte sizes, percentages, progress bars, and level colors.

These are pure formatting helpers -- they take a number and return a string.
They live in core because unrelated callers need the same output: the status
report draws the bars for `topo status` while ui.navigator draws them in the
Analyze rows, and every feature that prints a size prints it through
bytes_to_human. Keeping them here is what stopped the shared code from being
reached for through ui.navigator, which put a `core -> ui` edge in the module
graph and closed a cycle with the `ui -> core` edge every UI module already has.

ui.navigator re-exports these names, so `navigator.draw_bar` keeps working.
"""

from .constants import GREEN, RED, RESET, WHITE, YELLOW

BAR_FILLED = "▬"
BAR_EMPTY = "▬"
# Only when colors are off does the bar's *shape* have to carry the level:
# identical glyphs would make 0% and 100% render as the same solid bar.
BAR_EMPTY_NO_COLOR = "─"


def bytes_to_human(n_bytes: int) -> str:
    """Converts bytes to human readable format using binary units."""
    val = float(n_bytes)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if val < 1024:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} {unit}"
        val /= 1024
    return f"{val:.1f} PiB"


def get_color_for_percent(percent):
    """Returns the ANSI color code for a given percentage."""
    if percent > 80:
        return RED
    if percent > 50:
        return YELLOW
    return GREEN


def format_percent(percent, width=6):
    """Right-align a percentage, marking only the shares that round away to zero.

    A share that ``.1f`` would print as ``0.0%`` is rendered as ``<0.1%`` instead,
    so a small-but-present entry stays distinguishable from an empty one -- the
    same reason ``draw_bar`` keeps one block lit for any nonzero percentage.
    Exactly zero still prints ``0.0%``, the one case where that reading is
    literally true, and a share that rounds to ``0.1%`` keeps that number.

    The bound is decided by the rounded text rather than by a literal 0.05 so it
    cannot disagree with what the format itself does at the boundary.

    ``width`` counts the '%' sign, so the result always occupies the same number
    of columns as the ``100.0%`` it shares a field with.
    """
    rounded = f"{percent:.1f}"
    if percent > 0 and float(rounded) == 0:
        return f"{'<0.1%':>{width}}"
    return f"{rounded:>{width - 1}}%"


def draw_bar(percent, width=20, force_color=None):
    """Draws a sleek progress bar using the '▬' character.

    Filled and empty segments share the glyph and are told apart by color. When
    colors are disabled (--no-color / NO_COLOR / non-TTY) there is nothing left
    to tell them apart, so the empty track falls back to a lighter glyph instead
    of rendering every bar as full.
    """
    if width <= 0:
        return ""
    # Ensure even small percentages show at least one block to distinguish from 0%
    filled = int((percent / 100) * width)
    if percent > 0 and filled == 0:
        filled = 1
    filled = max(0, min(width, filled))
    empty = width - filled

    color = force_color or get_color_for_percent(percent)
    empty_glyph = BAR_EMPTY if RESET else BAR_EMPTY_NO_COLOR

    if filled == 0:
        # Empty track: neutral gray by default (high-contrast on dark and light
        # terminals), but an explicit force_color wins — a battery at 0% still
        # needs its red warning, and gray would read as "nothing to see here".
        return f"{force_color or WHITE}{empty_glyph * width}{RESET}"
    if empty == 0:
        return f"{color}{BAR_FILLED * width}{RESET}"

    return f"{color}{BAR_FILLED * filled}{RESET}{WHITE}{empty_glyph * empty}{RESET}"
