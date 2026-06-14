from ..core.constants import CYAN, EARTH, GRAY, RESET, TOPO_VERSION
from .navigator import InteractiveMenu

CLEAN_ACTION = "clean"
UNINSTALL_ACTION = "uninstall"
OPTIMIZE_ACTION = "optimize"
ANALYZE_ACTION = "analyze"
STATUS_ACTION = "status"
QUIT_ACTION = "quit"


def render_banner():
    # 2-line Braille vector typography for "TOPO" with version
    return f"""{EARTH}
 ⠶⣶⠶  ⢰⠶⡆ ⢰⠶⡆ ⢰⠶⡆
  ⠿   ⠸⠤⠇ ⢸⠉⠁ ⠸⠤⠇   {CYAN}●{RESET}{GRAY} v{TOPO_VERSION} is digging deeper 🦡{RESET}"""


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
