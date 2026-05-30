import subprocess
from pathlib import Path

from packaging.version import InvalidVersion, Version

from ..core.constants import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW


def _parse_version(version_text: str) -> Version | None:
    try:
        return Version(version_text.strip())
    except InvalidVersion:
        return None


def _should_update(local_version: str, remote_version: str) -> bool:
    local = _parse_version(local_version)
    remote = _parse_version(remote_version)
    return local is not None and remote is not None and remote > local


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
    local_parsed = _parse_version(local_version)
    remote_parsed = _parse_version(remote_version)
    if remote_parsed is None:
        print(f" {RED}❌ Invalid remote version: {remote_version!r}{RESET}")
        return
    if local_parsed is None:
        print(f" {RED}❌ Invalid local version: {local_version!r}{RESET}")
        return
    if remote_parsed == local_parsed:
        print(f" {GREEN}✓{RESET} {BOLD}Topo is already up to date!{RESET} (v{local_version})")
        return
    if remote_parsed < local_parsed:
        print(
            f" {GREEN}✓{RESET} {BOLD}Local Topo is newer than remote.{RESET} "
            f"(local: v{local_version}, remote: v{remote_version})"
        )
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
