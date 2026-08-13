import os
from pathlib import Path

from ..core.constants import BOLD, GRAY, GREEN, PURPLE, RED, RESET, YELLOW


def _get_link_target_dir() -> Path:
    if override := os.environ.get("TOPO_LINK_DIR"):
        return Path(override).expanduser()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/usr/local/bin")
    return Path.home() / ".local" / "bin"


def run_install_link(silent=False):
    """Creates a symbolic link for the topo launcher in a PATH-friendly bin dir."""

    if not silent:
        print(f"\n{PURPLE}☉ Setting up system-wide 'topo' command...{RESET}")

    # 1. Paths
    repo_root = Path(__file__).parent.parent.parent
    source_script = repo_root / "topo"
    target_dir = _get_link_target_dir()
    target_link = target_dir / "topo"

    if not source_script.exists():
        if not silent:
            print(f"  {RED}✗{RESET} Error: Could not find launcher script at {source_script}")
        return False

    # 2. Ensure target dir exists
    if not target_dir.exists():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if not silent:
                disp_dir = str(target_dir).replace(str(Path.home()), "~")
                print(f"  {GREEN}✓{RESET} {GRAY}Created directory {BOLD}{disp_dir}{RESET}")
        except OSError as e:
            if not silent:
                print(f"  {RED}✗{RESET} Error creating directory {target_dir}: {e}")
            return False

    # 3. Create/Update link atomically (temp symlink + os.replace), so an
    #    interrupted update never leaves the 'topo' command missing.
    try:
        tmp_link = target_link.with_name(f".{target_link.name}.topo-tmp")
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(source_script.absolute())
        os.replace(tmp_link, target_link)
        if not silent:
            disp_link = str(target_link).replace(str(Path.home()), "~")
            print(f"  {GREEN}✓{RESET} {GRAY}Executable linked to {BOLD}{disp_link}{RESET}")
    except OSError as e:
        if not silent:
            print(f"  {RED}✗{RESET} Error creating symbolic link: {e}")
            print(f"  {GRAY}You can still run topo directly with: {BOLD}{source_script}{RESET}")
        return False

    # 4. Path check (PATH auto-fix runs even in silent mode)
    path_env = os.environ.get("PATH", "")
    in_path = str(target_dir) in path_env.split(os.pathsep)
    added = False
    configured = False

    if not in_path:
        if not silent:
            print(f"\n {YELLOW}ℹ  {target_dir} is not in your PATH. Attempting auto-fix...{RESET}")

        shell_configs = [Path.home() / ".bashrc", Path.home() / ".zshrc"]
        if target_dir == Path.home() / ".local" / "bin":
            export_line = 'export PATH="$HOME/.local/bin:$PATH"'
        else:
            export_line = f'export PATH="{target_dir}:$PATH"'

        for config in shell_configs:
            if not config.exists():
                continue
            try:
                content = config.read_text()
                if export_line in content:
                    configured = True
                    continue
                with open(config, "a") as f:
                    f.write(f"\n# Added by topo\n{export_line}\n")
                if not silent:
                    print(f"  {GREEN}✓{RESET} Added to {GRAY}{config.name}{RESET}")
                added = True
                configured = True
            except OSError:
                pass

        if not silent:
            if added:
                print(f"\n {BOLD}Please restart your terminal or run:{RESET}")
                print(f" {GRAY}source ~/.bashrc{RESET} (or your shell config)")
            elif configured:
                print(f"\n {GRAY}PATH configuration already exists in your shell config.{RESET}")
                print(f" {BOLD}Please restart your terminal or run:{RESET}")
                print(f" {GRAY}source ~/.bashrc{RESET} (or your shell config)")
            else:
                print(f"\n {YELLOW}⚠️  Manual action required:{RESET}")
                print(" Add this line to your .bashrc or .zshrc:")
                print(f" {GRAY}{export_line}{RESET}")

    if not silent and (in_path or configured):
        print(
            f"\n  {GREEN}✓{RESET} {GRAY}System setup complete. '{BOLD}topo{RESET}{GRAY}' is ready to use!{RESET}"
        )

    return True
