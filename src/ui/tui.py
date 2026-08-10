from ..core.constants import GREEN, RESET, TOPO_VERSION, WHITE
from .navigator import InteractiveMenu

CLEAN_ACTION = "clean"
UNINSTALL_ACTION = "uninstall"
OPTIMIZE_ACTION = "optimize"
ANALYZE_ACTION = "analyze"
STATUS_ACTION = "status"
QUIT_ACTION = "quit"

STATUS_PULSE_COLORS = [
    "\033[1;36m",  # High-contrast Cyan
    "\033[1;32m",  # Active Green
    "\033[1;35m",  # Pulse Purple
]

_pulse_step = 0


def render_banner():
    """Renders professional industrial monochrome TUI header for TOPO."""
    global _pulse_step
    _pulse_step = (_pulse_step + 1) % len(STATUS_PULSE_COLORS)
    pulse = STATUS_PULSE_COLORS[_pulse_step]

    # Professional clean monochrome ASCII typography with single active status dot pulse
    return f"""
 {GREEN}⠶⣶⠶  ⢰⠶⡆ ⢰⠶⡆ ⢰⠶⡆{RESET}
  {GREEN}⠿   ⠸⠤⠇ ⢸⠉⠁ ⠸⠤⠇{RESET}   {pulse}●{RESET}{WHITE} v{TOPO_VERSION} is digging deeper 🦡{RESET}"""


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
