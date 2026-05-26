import os
import shutil
import sys
import time
from pathlib import Path
from ..core.system import get_invoking_user
from ..core.file_ops import bytes_to_human, get_size

def run_self_uninstall(dry_run=False):
    """Removes topo from the system."""
    # ANSI Colors
    MAGENTA = "\033[1;35m"
    GREEN = "\033[1;32m"
    GRAY = "\033[1;90m"
    RESET = "\033[0m"
    BOLD = "\033[1m"

    print(f"\n {MAGENTA}☉ Removing topo from your system...{RESET}\n")

    # 1. Identify files to remove
    to_remove = []
    
    # The launcher link
    launcher_path = Path.home() / ".local/bin/topo"
    if launcher_path.exists():
        to_remove.append({
            "path": launcher_path,
            "desc": "Launcher script link",
            "type": "link"
        })

    # Configuration directory
    config_dir = Path.home() / ".config" / "topo"
    if config_dir.exists():
        to_remove.append({
            "path": config_dir,
            "desc": "Configuration and whitelist",
            "type": "dir"
        })

    # Cache directory (if any)
    cache_dir = Path.home() / ".cache" / "topo"
    if cache_dir.exists():
        to_remove.append({
            "path": cache_dir,
            "desc": "Temporary scan cache",
            "type": "dir"
        })

    # Internal installation directory (from install.sh)
    internal_dir = Path.home() / ".topo"
    if internal_dir.exists():
        to_remove.append({
            "path": internal_dir,
            "desc": "Main program files",
            "type": "dir"
        })

    if not to_remove:
        print(f" {GREEN}✓{RESET} No system integration found to remove.")
        return

    # Calculate total size
    total_size = sum(get_size(item['path']) for item in to_remove)
    
    # 2. Preview
    print(f" {BOLD}The following items will be removed:{RESET}")
    for item in to_remove:
        size_str = bytes_to_human(get_size(item['path']))
        print(f"  \033[1;32m✓\033[0m {str(item['path']).replace(str(Path.home()), '~'):<40} {GRAY}({item['desc']}, {size_str}){RESET}")

    if dry_run:
        print(f"\n {GREEN}✓{RESET} Dry run complete. Total to free: {bytes_to_human(total_size)}")
        return

    # 3. Confirmation (Mole-style)
    print(f"\n \033[1;35m→\033[0m Remove topo, {bytes_to_human(total_size)}  \033[1;32mEnter\033[0m confirm, \033[1;90mESC\033[0m cancel: ", end="", flush=True)

    # Single-key capture
    import tty, termios
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    if ch not in ('\r', '\n', 'y', 'Y'):
        print(f"\n\n {GRAY}Uninstallation cancelled.{RESET}")
        return

    # 4. Execution
    print("\n")
    for item in to_remove:
        p = item['path']
        try:
            if item['type'] == "dir":
                shutil.rmtree(p)
            else:
                p.unlink()
            print(f"  \033[0;32m✓\033[0m Removed {item['desc']}")
        except Exception as e:
            print(f"  \033[1;31m✗\033[0m Failed to remove {p}: {e}")

    print("\n" + "=" * 70)
    print(f" \033[1;34mtopo has been removed from your system.{RESET}")
    print("=" * 70 + "\n")
