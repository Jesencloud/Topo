import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from ..core import terminal_state
from ..core.constants import BOLD, GRAY, GREEN, MAGENTA, PURPLE, RED, RESET, YELLOW
from ..core.file_ops import bytes_to_human, get_size_fast, safe_remove
from ..core.install_source import (
    PACKAGE_INSTALL,
    get_install_source,
    get_package_remove_argv,
)


class _RemoveItem(TypedDict, total=False):
    path: Path
    desc: str
    type: str
    size: int


def _resolve_launcher_symlink(launcher: Path) -> Path | None:
    """Resolve a launcher symlink to its real target, or None on failure."""
    try:
        raw = Path(os.readlink(launcher))
        return (raw if raw.is_absolute() else launcher.parent / raw).resolve()
    except (OSError, ValueError):
        return None


def _launcher_points_to_topo(launcher_path: Path, internal_dir: Path) -> bool:
    """True if the launcher is Topo's link, even when dangling (target removed)."""
    try:
        expected = (internal_dir / "topo").resolve()
    except OSError:
        expected = internal_dir / "topo"
    if launcher_path.is_symlink():
        resolved = _resolve_launcher_symlink(launcher_path)
        if not resolved:
            # Fallback for dangling symlink string match
            raw = Path(os.readlink(launcher_path))
            target = raw if raw.is_absolute() else launcher_path.parent / raw
            return os.path.normpath(target) == os.path.normpath(expected)
        return resolved == expected or str(resolved) == str(expected)
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
        resolved = _resolve_launcher_symlink(launcher_path)
        if not resolved:
            return False
        return str(resolved) in {
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
    ok, _ = safe_remove(
        path,
        use_trash=False,
        allow_app_data_removal=True,
        allow_self_removal=True,
    )
    return ok


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
            process = subprocess.run(command, timeout=300)
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

    # 1. Identify files to remove
    to_remove: list[_RemoveItem] = []

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
    total_size: int = sum(item["size"] for item in to_remove)

    # 2. Preview (Compact Header)
    print(f"\n{PURPLE} ●{RESET} The following items will be removed:{RESET}")
    for item in to_remove:
        size_str = bytes_to_human(int(item["size"]))
        disp_path = str(item["path"]).replace(str(Path.home()), "~")
        print(f"  {GREEN}✓{RESET} {GRAY}{disp_path:<40} ({item['desc']}, {size_str}){RESET}")

    if dry_run:
        print(
            f"\n {GREEN}✓{RESET} {GRAY}Dry run complete. Total to free: {bytes_to_human(total_size)}{RESET}"
        )
        print(f"  {GRAY}(Shell PATH entries added by topo would also be removed.){RESET}")
        return

    # 3. Confirmation (Mole-style)
    print(
        f"\n {PURPLE}●{RESET} Remove topo ({bytes_to_human(total_size)}): Press {GREEN}Enter{RESET} confirm, {GREEN}ESC{RESET} cancel: ",
        end="",
        flush=True,
    )

    # Single-key capture
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    terminal_state.remember_raw_state(fd, old_settings)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        terminal_state.restore_raw_state(fd, old_settings)

    if ch not in ("\r", "\n", "y", "Y"):
        print(f"\n\n {YELLOW}⚠{RESET}{GRAY} Uninstallation cancelled.{RESET}")
        return

    # 4. Execution
    print()
    had_errors = False
    for item in to_remove:
        p: Path = item["path"]
        ok, reason = safe_remove(
            p,
            use_trash=False,
            allow_app_data_removal=True,
            allow_self_removal=True,
        )
        if ok:
            print(f"  {GREEN}✓{RESET} {GRAY}Removed {item['desc']}{RESET}")
        else:
            had_errors = True
            print(f"  {RED}✗{RESET} Failed to remove {p}: {reason}")

    # Deletion auditing can recreate XDG_STATE_HOME/topo, while protection
    # checks can recreate ~/.config/topo to read an empty whitelist after the
    # original directory was removed. Clear both self-generated directories
    # once all uninstall work is finished.
    config_dir = Path.home() / ".config" / "topo"
    state_dir = (
        Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))).expanduser()
        / "topo"
    )
    for generated_dir in (config_dir, state_dir):
        if not generated_dir.exists():
            continue
        try:
            shutil.rmtree(generated_dir)
        except OSError as e:
            had_errors = True
            print(f"  {RED}✗{RESET} Failed to remove {generated_dir}: {e}")

    if _strip_topo_path_lines():
        print(f"  {GREEN}✓{RESET} {GRAY}Removed PATH entry from shell config{RESET}")

    if had_errors:
        print(f"\n {YELLOW}⚠{RESET} {GRAY}Topo removal completed with errors (see above).{RESET}\n")
    else:
        print(f"\n {GREEN}✨ Topo has been successfully removed from your system!{RESET}\n")
