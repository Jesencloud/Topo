from ..core.constants import GREEN, RESET, TOPO_VERSION, WHITE
from .navigator import InteractiveMenu

CLEAN_ACTION = "clean"
UNINSTALL_ACTION = "uninstall"
OPTIMIZE_ACTION = "optimize"
ANALYZE_ACTION = "analyze"
STATUS_ACTION = "status"
QUIT_ACTION = "quit"


def render_banner():
    """Renders professional industrial monochrome TUI header for TOPO."""
    # The dot used to cycle cyan -> green -> purple once per render_banner() call.
    # render_banner() runs on every full redraw, i.e. on every keystroke, so the
    # color tracked how fast the user was typing rather than any state of the
    # program -- and because the palette lived in a module-level list of inline
    # SGR literals, constants._propagate_colors() could not reach it and the dot
    # stayed colored under --no-color. One color, from constants, fixes both.
    return f"""
 {GREEN}⠶⣶⠶  ⢰⠶⡆ ⢰⠶⡆ ⢰⠶⡆{RESET}
  {GREEN}⠿   ⠸⠤⠇ ⢸⠉⠁ ⠸⠤⠇{RESET}   {GREEN}●{RESET}{WHITE} v{TOPO_VERSION} is digging deeper 🦡{RESET}"""


def main_menu():
    options = [
        (CLEAN_ACTION, "1. Clean", "Free up disk space"),
        (UNINSTALL_ACTION, "2. Uninstall", "Remove apps completely"),
        (OPTIMIZE_ACTION, "3. Optimize", "Check and maintain system"),
        (ANALYZE_ACTION, "4. Analyze", "Explore disk usage"),
        (STATUS_ACTION, "5. Status", "Monitor system health"),
    ]

    menu = InteractiveMenu(
        "Main Menu",
        [(label, desc) for _, label, desc in options],
        show_banner=render_banner,
    )
    choice_idx = menu.run()

    if choice_idx is None:
        return QUIT_ACTION

    return options[choice_idx][0]
