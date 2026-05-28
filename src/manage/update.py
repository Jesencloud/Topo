import subprocess
from pathlib import Path

from ..core.constants import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW


def run_update():
    """Updates topo by re-running the official installation script with version check."""

    # 1. Get current local version
    # Since we are running from src/manage/update.py,
    # the VERSION file should be in the root of the installation (~/.topo/VERSION)
    install_dir = Path(__file__).parent.parent.parent
    version_file = install_dir / "VERSION"
    local_version = "0.0.0"
    if version_file.exists():
        local_version = version_file.read_text().strip()

    print(f" {CYAN}🚀 Checking for updates...{RESET} (Local: v{local_version})")

    # 2. Fetch remote version
    remote_version_url = "https://raw.githubusercontent.com/Jesencloud/Topo/main/VERSION"
    try:
        remote_version = subprocess.check_output(
            ["curl", "-fsSL", remote_version_url], text=True
        ).strip()
    except Exception as e:
        print(f" {RED}❌ Failed to check remote version: {e}{RESET}")
        return

    # 3. Compare and act
    if local_version == remote_version:
        print(f" {GREEN}✓{RESET} {BOLD}Topo is already up to date!{RESET} (v{local_version})")
        return

    print(f" {YELLOW}⬆️  New version available: v{remote_version}{RESET}")
    print(f" {GRAY}Updating Topo from v{local_version} to v{remote_version}...{RESET}\n")

    # 4. Run update script in minimal mode
    # We pass --minimal as an argument to bash -s
    install_cmd = "curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash -s -- --minimal"

    try:
        process = subprocess.run(install_cmd, shell=True)

        if process.returncode == 0:
            print(f"\n {GREEN}✨ Topo has been successfully updated to v{remote_version}!{RESET}")
        else:
            print(f"\n {RED}❌ Update failed with exit code {process.returncode}{RESET}")

    except Exception as e:
        print(f"\n {RED}❌ Error during update: {e}{RESET}")
