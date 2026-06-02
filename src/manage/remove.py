import shutil
import sys
from pathlib import Path

from ..core.constants import BLUE, BOLD, GRAY, GREEN, MAGENTA, RED, RESET
from ..core.file_ops import bytes_to_human, get_size


def run_remove(dry_run=False):
    """Removes topo from the system."""

    print(f"\n {MAGENTA}☉ Removing topo from your system...{RESET}\n")

    # 1. Identify files to remove
    to_remove = []

    # The launcher link
    internal_dir = Path.home() / ".topo"
    for launcher_path in (Path.home() / ".local/bin/topo", Path("/usr/local/bin/topo")):
        if launcher_path.exists() or launcher_path.is_symlink():
            try:
                if launcher_path.resolve() != (internal_dir / "topo").resolve():
                    continue
            except OSError:
                continue
            to_remove.append(
                {"path": launcher_path, "desc": "Launcher script link", "type": "link"}
            )

    # Configuration directory
    config_dir = Path.home() / ".config" / "topo"
    if config_dir.exists():
        to_remove.append({"path": config_dir, "desc": "Configuration and whitelist", "type": "dir"})

    # Cache directory (if any)
    cache_dir = Path.home() / ".cache" / "topo"
    if cache_dir.exists():
        to_remove.append({"path": cache_dir, "desc": "Temporary scan cache", "type": "dir"})

    # Internal installation directory (from install.sh)
    if internal_dir.exists():
        to_remove.append({"path": internal_dir, "desc": "Main program files", "type": "dir"})

    if not to_remove:
        print(f" {GREEN}✓{RESET} No system integration found to remove.")
        return

    # Calculate total size
    total_size = sum(get_size(item["path"]) for item in to_remove)

    # 2. Preview
    print(f" {BOLD}The following items will be removed:{RESET}")
    for item in to_remove:
        size_str = bytes_to_human(get_size(item["path"]))
        print(
            f"  {GREEN}✓{RESET} {str(item['path']).replace(str(Path.home()), '~'):<40} {GRAY}({item['desc']}, {size_str}){RESET}"
        )

    if dry_run:
        print(f"\n {GREEN}✓{RESET} Dry run complete. Total to free: {bytes_to_human(total_size)}")
        return

    # 3. Confirmation (Mole-style)
    print(
        f"\n {MAGENTA}→{RESET} Remove topo, {bytes_to_human(total_size)}  {GREEN}Enter{RESET} confirm, {GRAY}ESC{RESET} cancel: ",
        end="",
        flush=True,
    )

    # Single-key capture
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(sys.stdin.fileno())
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    if ch not in ("\r", "\n", "y", "Y"):
        print(f"\n\n {GRAY}Uninstallation cancelled.{RESET}")
        return

    # 4. Execution
    print("\n")
    for item in to_remove:
        p = item["path"]
        try:
            if item["type"] == "dir":
                shutil.rmtree(p)
            else:
                p.unlink()
            print(f"  {GREEN}✓{RESET} Removed {item['desc']}")
        except Exception as e:
            print(f"  {RED}✗{RESET} Failed to remove {p}: {e}")

    print("\n" + "=" * 70)
    print(f" {BLUE}topo has been removed from your system.{RESET}")
    print("=" * 70 + "\n")
