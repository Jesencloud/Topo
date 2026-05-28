import os
from pathlib import Path

from ..core.constants import BLUE, BOLD, CYAN, GRAY, GREEN, RESET, YELLOW


def run_install_link(silent=False):
    """Creates a symbolic link for topo in ~/.local/bin."""

    if not silent:
        print(f"\n {CYAN}☉ Setting up system-wide 'topo' command...{RESET}\n")

    # 1. Paths
    repo_root = Path(__file__).parent.parent.parent
    source_script = repo_root / "topo"
    target_dir = Path.home() / ".local" / "bin"
    target_link = target_dir / "topo"

    if not source_script.exists():
        if not silent:
            print(f" {YELLOW}✗{RESET} Error: Could not find launcher script at {source_script}")
        return

    # 2. Ensure target dir exists
    if not target_dir.exists():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if not silent:
                print(f"  {GREEN}✓{RESET} Created directory: {GRAY}~/.local/bin{RESET}")
        except Exception as e:
            if not silent:
                print(f" {YELLOW}✗{RESET} Error creating directory {target_dir}: {e}")
            return

    # 3. Create/Update link
    try:
        if target_link.exists() or target_link.is_symlink():
            target_link.unlink()
            if not silent:
                print(f"  {GRAY}↺{RESET} Removed existing link at {target_link}")

        target_link.symlink_to(source_script.absolute())
        if not silent:
            print(f"  {GREEN}✓{RESET} Created symbolic link: {BOLD}{target_link}{RESET}")
    except Exception as e:
        if not silent:
            print(f" {YELLOW}✗{RESET} Error creating symbolic link: {e}")
        return

    # 4. Success message & Path check
    if silent:
        return

    print("\n" + "=" * 70)
    print(f" {BLUE}Success! 'topo' is now available.{RESET}")

    path_env = os.environ.get("PATH", "")
    if str(target_dir) not in path_env:
        print(f"\n {YELLOW}ℹ  ~/.local/bin is not in your PATH. Attempting auto-fix...{RESET}")

        added = False
        # Potential shell config files
        shell_configs = [Path.home() / ".bashrc", Path.home() / ".zshrc"]
        export_line = 'export PATH="$HOME/.local/bin:$PATH"'

        for config in shell_configs:
            if config.exists():
                try:
                    content = config.read_text()
                    if export_line not in content:
                        with open(config, "a") as f:
                            f.write(f"\n# Added by topo\n{export_line}\n")
                        print(f"  {GREEN}✓{RESET} Added to {GRAY}{config.name}{RESET}")
                        added = True
                except Exception:
                    pass

        if added:
            print(f"\n {BOLD}Please restart your terminal or run:{RESET}")
            print(f" {GRAY}source ~/.bashrc{RESET} (or your shell config)")
        else:
            print(f"\n {YELLOW}⚠️  Manual action required:{RESET}")
            print(" Add this line to your .bashrc or .zshrc:")
            print(f" {GRAY}{export_line}{RESET}")
    else:
        print(f" You can now run {BOLD}topo{RESET} from any directory.")
    print("=" * 70 + "\n")
