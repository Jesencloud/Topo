from ..core.constants import EARTH, GREEN, RESET, TOPO_VERSION, WHITE
from .navigator import InteractiveMenu

CLEAN_ACTION = "clean"
UNINSTALL_ACTION = "uninstall"
OPTIMIZE_ACTION = "optimize"
ANALYZE_ACTION = "analyze"
STATUS_ACTION = "status"
QUIT_ACTION = "quit"


def render_banner():
    """Renders professional industrial monochrome TUI header for TOPO."""
    return f"""
 {EARTH}⠶⣶⠶  ⢰⠶⡆ ⢰⠶⡆ ⢰⠶⡆{RESET}
  {EARTH}⠿   ⠸⠤⠇ ⢸⠉⠁ ⠸⠤⠇{RESET}   {GREEN}●{RESET}{WHITE} v{TOPO_VERSION} is digging deeper 🦡{RESET}"""


def main_menu(selected_index=0):
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
    menu.selected_index = max(0, min(selected_index, len(menu.options) - 1))
    choice_idx = menu.run()

    if choice_idx is None:
        return QUIT_ACTION

    return options[choice_idx][0]
