import os
import sys
from pathlib import Path

def run_install_link():
    """Creates a symbolic link for topo in ~/.local/bin."""
    # ANSI Colors
    CYAN = "\033[1;36m"
    GREEN = "\033[1;32m"
    YELLOW = "\033[1;33m"
    GRAY = "\033[1;90m"
    RESET = "\033[0m"
    BOLD = "\033[1m"

    print(f"\n {CYAN}☉ Setting up system-wide 'topo' command...{RESET}\n")

    # 1. Paths
    repo_root = Path(__file__).parent.parent.parent
    source_script = repo_root / "topo"
    target_dir = Path.home() / ".local" / "bin"
    target_link = target_dir / "topo"

    if not source_script.exists():
        print(f" {YELLOW}✗{RESET} Error: Could not find launcher script at {source_script}")
        return

    # 2. Ensure target dir exists
    if not target_dir.exists():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            print(f"  {GREEN}✓{RESET} Created directory: {GRAY}~/.local/bin{RESET}")
        except Exception as e:
            print(f" {YELLOW}✗{RESET} Error creating directory {target_dir}: {e}")
            return

    # 3. Create/Update link
    try:
        if target_link.exists() or target_link.is_symlink():
            target_link.unlink()
            print(f"  {GRAY}↺{RESET} Removed existing link at {target_link}")
        
        target_link.symlink_to(source_script.absolute())
        print(f"  {GREEN}✓{RESET} Created symbolic link: {BOLD}{target_link}{RESET}")
    except Exception as e:
        print(f" {YELLOW}✗{RESET} Error creating symbolic link: {e}")
        return

    # 4. Success message & Path check
    print("\n" + "=" * 70)
    print(f" \033[1;34mSuccess! 'topo' is now available.{RESET}")
    
    path_env = os.environ.get("PATH", "")
    if str(target_dir) not in path_env:
        print(f"\n {YELLOW}⚠️  Warning:{RESET} {BOLD}~/.local/bin{RESET} is not in your PATH.")
        print(f" Please add it to your shell config (e.g., .bashrc or .zshrc):")
        print(f" {GRAY}export PATH=\"$HOME/.local/bin:$PATH\"{RESET}")
    else:
        print(f" You can now run {BOLD}topo{RESET} from any directory.")
    print("=" * 70 + "\n")
