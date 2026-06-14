import os
import sys

from ..core.constants import CYAN, EARTH, GRAY, RESET, TOPO_VERSION
from .navigator import InteractiveMenu


def show_banner():
    # Detect the calling command name
    cmd_name = os.path.basename(sys.argv[0])
    if cmd_name in ("python3", "main.py", "topo"):
        cmd_name = "Topo"

    # 2-line Braille vector typography for "TOPO" with version
    banner = f"""{EARTH}
 ⠶⣶⠶  ⢰⠶⡆ ⢰⠶⡆ ⢰⠶⡆
  ⠿   ⠸⠤⠇ ⢸⠉⠁ ⠸⠤⠇  {CYAN}●{RESET}{GRAY} 🦡 ({GRAY}v{TOPO_VERSION}) is digging deeper{RESET}"""
    print(banner)


def main_menu():
    options = [
        ("1. Clean", "Free up disk space"),
        ("2. Uninstall", "Remove apps completely"),
        ("3. Optimize", "Check and maintain system"),
        ("4. Analyze", "Explore disk usage"),
        ("5. Status", "Monitor system health"),
    ]

    menu = InteractiveMenu("Main Menu", options, show_banner=show_banner)
    choice_idx = menu.run()

    if choice_idx is None:
        return "0"

    return str(choice_idx + 1)
