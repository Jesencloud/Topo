import os
import sys

# ANSI Colors
BLUE = "\033[1;34m"
CYAN = "\033[1;36m"
MAGENTA = "\033[1;35m"
YELLOW = "\033[1;33m"
WHITE = "\033[1;37m"
GRAY = "\033[1;90m"
RESET = "\033[0m"
BOLD = "\033[1m"

def show_banner():
    # Detect the calling command name
    cmd_name = os.path.basename(sys.argv[0])
    if cmd_name in ("lmo.py", "python3", "main.py"): cmd_name = "lmole"
    
    banner = f"""{BLUE} _                           _      
| |                         | |     
| |   _ __ ___     ___      | |   ___
| |  | '_ ` _ \\   / _ \\     | |  / _ \\
| |____  | | | | | | | (_) |    | | |  __/
|______| |_| |_| |_|  \\___/     |_|  \\___|{RESET}"""
    
    print(banner)
    print(f"{WHITE}      v0.5.0 | {cmd_name} - Linux System Optimizer{RESET}")

from .navigator import InteractiveMenu, Navigator

def main_menu():
    options = [
        ("1. Clean", "One-key safe disk cleanup"),
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
