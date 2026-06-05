import os
import shutil
import sys
from pathlib import Path

from ..core.constants import BLUE, BOLD, GRAY, GREEN, MAGENTA, RED, RESET
from ..core.file_ops import bytes_to_human, get_size
from ..core.install_source import (
    PACKAGE_INSTALL,
    get_install_source,
    get_package_manager_commands,
)


def _launcher_points_to_topo(launcher_path: Path, internal_dir: Path) -> bool:
    """True if the launcher is Topo's link, even when dangling (target removed)."""
    try:
        expected = (internal_dir / "topo").resolve()
    except OSError:
        expected = internal_dir / "topo"
    if launcher_path.is_symlink():
        raw = Path(os.readlink(launcher_path))
        target = raw if raw.is_absolute() else launcher_path.parent / raw
        try:
            resolved = target.resolve()
        except OSError:
            resolved = target
        return resolved == expected or os.path.normpath(target) == os.path.normpath(expected)
    try:
        return launcher_path.resolve() == expected
    except OSError:
        return False


def _strip_topo_path_lines() -> bool:
    """Remove the `# Added by topo` PATH-export block from shell rc files."""
    marker = "# Added by topo"
    changed = False
    for config in (Path.home() / ".bashrc", Path.home() / ".zshrc"):
        if not config.exists():
            continue
        try:
            original = config.read_text().splitlines()
        except OSError:
            continue
        cleaned: list[str] = []
        drop_export = False
        for line in original:
            if line.strip() == marker:
                drop_export = True
                continue
            if drop_export and line.strip().startswith("export PATH="):
                drop_export = False
                continue
            drop_export = False
            cleaned.append(line)
        if cleaned != original:
            try:
                config.write_text("\n".join(cleaned) + ("\n" if cleaned else ""))
                changed = True
            except OSError:
                pass
    return changed


def run_remove(dry_run=False):
    """Removes topo from the system."""

    if get_install_source() == PACKAGE_INSTALL:
        print(f"\n {MAGENTA}☉ Topo was installed by a system package manager.{RESET}\n")
        print(f" {GRAY}Use your package manager to remove it:{RESET}")
        for command in get_package_manager_commands("remove"):
            print(f"   {BOLD}{command}{RESET}")
        return

    print(f"\n {MAGENTA}☉ Removing topo from your system...{RESET}\n")

    # 1. Identify files to remove
    to_remove = []

    # The launcher link
    internal_dir = Path.home() / ".topo"
    for launcher_path in (Path.home() / ".local/bin/topo", Path("/usr/local/bin/topo")):
        if (launcher_path.exists() or launcher_path.is_symlink()) and _launcher_points_to_topo(
            launcher_path, internal_dir
        ):
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

    # Deletion-audit / state directory (XDG_STATE_HOME/topo)
    state_dir = (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))).expanduser()
        / "topo"
    )
    if state_dir.exists():
        to_remove.append({"path": state_dir, "desc": "Deletion history / state", "type": "dir"})

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
        print(f" {GRAY}(Shell PATH entries added by topo would also be removed.){RESET}")
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

    if _strip_topo_path_lines():
        print(f"  {GREEN}✓{RESET} Removed PATH entry from shell config")

    print("\n" + "=" * 70)
    print(f" {BLUE}topo has been removed from your system.{RESET}")
    print("=" * 70 + "\n")
