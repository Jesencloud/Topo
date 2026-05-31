import json
import subprocess
from pathlib import Path

from packaging.version import InvalidVersion, Version

from ..core.constants import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW


def _parse_version(version_text: str) -> Version | None:
    try:
        return Version(version_text.strip().lstrip("vV"))
    except InvalidVersion:
        return None


def _should_update(local_version: str, remote_version: str) -> bool:
    local = _parse_version(local_version)
    remote = _parse_version(remote_version)
    return local is not None and remote is not None and remote > local


def _fetch_latest_release_tag() -> str:
    latest_release_url = "https://api.github.com/repos/Jesencloud/Topo/releases/latest"
    data = subprocess.check_output(["curl", "-fsSL", latest_release_url], text=True)
    tag = json.loads(data).get("tag_name", "")
    if not isinstance(tag, str):
        return ""
    return tag.strip()


def run_update():
    """Updates topo from the latest GitHub Release when its tag is newer."""

    # 1. Get current local version
    # Since we are running from src/manage/update.py,
    # the VERSION file should be in the root of the installation (~/.topo/VERSION)
    install_dir = Path(__file__).parent.parent.parent
    version_file = install_dir / "VERSION"
    local_version = "0.0.0"
    if version_file.exists():
        local_version = version_file.read_text().strip()

    print(f" {CYAN}🚀 Checking for updates...{RESET} (Local: v{local_version})")

    # 2. Fetch latest stable release tag
    try:
        remote_tag = _fetch_latest_release_tag()
    except Exception as e:
        print(f" {RED}❌ Failed to check latest release: {e}{RESET}")
        return

    # 3. Compare and act
    local_parsed = _parse_version(local_version)
    remote_parsed = _parse_version(remote_tag)
    if remote_parsed is None:
        print(f" {RED}❌ Invalid release tag: {remote_tag!r}{RESET}")
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
            f"(local: v{local_version}, remote: {remote_tag})"
        )
        return

    print(f" {YELLOW}⬆️  New version available: {remote_tag}{RESET}")
    print(f" {GRAY}Updating Topo from v{local_version} to {remote_tag}...{RESET}\n")

    # 4. Run update script in minimal mode
    # Install from the release tag instead of the development branch.
    install_cmd = (
        "curl -fsSL "
        f"https://raw.githubusercontent.com/Jesencloud/Topo/{remote_tag}/install.sh "
        f"| bash -s -- --minimal --version {remote_tag}"
    )

    try:
        process = subprocess.run(install_cmd, shell=True)

        if process.returncode == 0:
            print(f"\n {GREEN}✨ Topo has been successfully updated to {remote_tag}!{RESET}")
        else:
            print(f"\n {RED}❌ Update failed with exit code {process.returncode}{RESET}")

    except Exception as e:
        print(f"\n {RED}❌ Error during update: {e}{RESET}")
