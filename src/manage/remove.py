import os
import shutil
import subprocess
import sys
from pathlib import Path

from ..core.constants import BLUE, BOLD, GRAY, GREEN, MAGENTA, RED, RESET
from ..core.file_ops import bytes_to_human, get_size
from ..core.install_source import (
    PACKAGE_INSTALL,
    get_install_source,
    get_package_remove_argv,
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
    except (OSError, UnicodeDecodeError):
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


def _launcher_points_to_package(launcher_path: Path) -> bool:
    if launcher_path.is_symlink():
        raw = Path(os.readlink(launcher_path))
        target = raw if raw.is_absolute() else launcher_path.parent / raw
        normalized = os.path.normpath(target)
        return normalized in {
            os.path.normpath("/usr/bin/topo"),
            os.path.normpath("/usr/lib/topo/topo"),
        }
    try:
        return (
            launcher_path.is_file()
            and "Managed by topo package compatibility launcher" in launcher_path.read_text()
        )
    except (OSError, UnicodeDecodeError):
        return False


def _remove_path(path: Path) -> bool:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        elif path.exists() or path.is_symlink():
            path.unlink()
        else:
            return False
    except OSError:
        return False
    return True


def _remove_package_user_residue() -> list[str]:
    removed: list[str] = []
    home = Path.home()
    internal_dir = home / ".topo"
    launcher_path = home / ".local/bin/topo"

    if (
        (launcher_path.exists() or launcher_path.is_symlink())
        and (
            _launcher_points_to_topo(launcher_path, internal_dir)
            or _launcher_points_to_package(launcher_path)
        )
        and _remove_path(launcher_path)
    ):
        removed.append("Launcher compatibility entry")

    for path, label in (
        (internal_dir, "Script install directory"),
        (home / ".config/topo", "Configuration and whitelist"),
        (home / ".cache/topo", "Temporary scan cache"),
        (
            Path(os.environ.get("XDG_STATE_HOME", str(home / ".local/state"))).expanduser()
            / "topo",
            "Deletion history / state",
        ),
    ):
        if _remove_path(path):
            removed.append(label)

    if _strip_topo_path_lines():
        removed.append("Shell PATH entry")

    return removed


def run_remove(dry_run=False):
    """Removes topo from the system."""

    if get_install_source() == PACKAGE_INSTALL:
        command = get_package_remove_argv()
        if not command:
            print(f"\n {RED}✗ Unsupported Linux distribution for package removal.{RESET}")
            return
        print(f"\n {MAGENTA}☉ Removing Topo through the system package manager.{RESET}\n")
        print(f" {GRAY}Running:{RESET} {BOLD}{' '.join(command)}{RESET}")
        if dry_run:
            print(f" {GREEN}✓{RESET} Dry run complete. Package removal command was not executed.")
            return
        try:
            process = subprocess.run(command)
        except (OSError, subprocess.SubprocessError) as e:
            print(f" {RED}✗ Package removal failed: {e}{RESET}")
            return
        if process.returncode == 0:
            print(f"\n {GREEN}✓{RESET} Topo package removal completed.")
            for label in _remove_package_user_residue():
                print(f"  {GREEN}✓{RESET} Removed {label}")
            print(
                f" {GRAY}If your shell still uses an old command path, run:{RESET} {BOLD}hash -r{RESET}"
            )
        else:
            print(f"\n {RED}✗ Package removal failed with exit code {process.returncode}.{RESET}")
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

    # Calculate total size and prepare detailed list
    for item in to_remove:
        item["size"] = get_size_fast(item["path"])
    total_size = sum(item["size"] for item in to_remove)

    # 2. Preview
    print(f" {BOLD}The following items will be removed:{RESET}")
    for item in to_remove:
        size_str = bytes_to_human(item["size"])
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
