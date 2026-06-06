import json
import re
import subprocess
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from packaging.version import InvalidVersion, Version

from ..core.constants import BOLD, CYAN, GRAY, GREEN, RED, RESET, YELLOW
from ..core.install_source import (
    PACKAGE_INSTALL,
    get_install_source,
    get_package_asset_name,
    get_package_upgrade_argv,
)


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
    try:
        data = subprocess.check_output(
            [
                "curl",
                "-fsSL",
                "-H",
                "Accept: application/vnd.github+json",
                "-H",
                "User-Agent: topo-updater",
                latest_release_url,
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
        tag = json.loads(data).get("tag_name", "")
        if isinstance(tag, str) and tag.strip():
            return tag.strip()
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass

    latest_redirect_url = subprocess.check_output(
        [
            "curl",
            "-fsSLI",
            "-o",
            "/dev/null",
            "-w",
            "%{url_effective}",
            "-A",
            "topo-updater",
            "https://github.com/Jesencloud/Topo/releases/latest",
        ],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=15,
    )
    return latest_redirect_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0].strip()


def _read_local_version() -> str:
    install_dir = Path(__file__).parent.parent.parent
    version_file = install_dir / "VERSION"
    if version_file.exists():
        return version_file.read_text().strip()
    return "0.0.0"


def _release_download_url(tag: str, asset_name: str) -> str:
    return f"https://github.com/Jesencloud/Topo/releases/download/{tag}/{asset_name}"


def _download_file(url: str, destination: Path, timeout: int = 60) -> None:
    subprocess.check_call(
        ["curl", "-fL", "--retry", "2", "-A", "topo-updater", url, "-o", str(destination)],
        timeout=timeout,
    )


def _expected_sha256(sha256sums_path: Path, asset_name: str) -> str | None:
    for line in sha256sums_path.read_text().splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        checksum, filename = parts
        if Path(filename.lstrip("*")).name == asset_name:
            return checksum.lower()
    return None


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_release_checksum(package_path: Path, sha256sums_path: Path) -> bool:
    expected = _expected_sha256(sha256sums_path, package_path.name)
    if not expected:
        print(f" {RED}❌ SHA256SUMS does not list {package_path.name}.{RESET}")
        return False
    actual = _file_sha256(package_path)
    if actual != expected:
        print(f" {RED}❌ Checksum mismatch for {package_path.name}.{RESET}")
        print(f" {GRAY}Expected: {expected}{RESET}")
        print(f" {GRAY}Actual:   {actual}{RESET}")
        return False
    print(f" {GREEN}✓{RESET} Verified SHA256 for {package_path.name}")
    return True


def _run_package_update(local_version: str, remote_tag: str) -> None:
    asset_name = get_package_asset_name(remote_tag)
    if not asset_name:
        print(f" {RED}❌ Unsupported Linux distribution for package updates.{RESET}")
        return

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", remote_tag):
        print(f" {RED}❌ Refusing unsafe release tag: {remote_tag!r}{RESET}")
        return

    print(f" {YELLOW}⬆️  New package available: {remote_tag}{RESET}")
    print(f" {GRAY}Updating Topo from v{local_version} to {remote_tag}...{RESET}\n")

    with TemporaryDirectory(prefix="topo-update-") as temp_dir:
        download_dir = Path(temp_dir)
        package_path = download_dir / asset_name
        sha256sums_path = download_dir / "SHA256SUMS"

        try:
            print(f" {GRAY}↓ Downloading {asset_name}...{RESET}")
            _download_file(_release_download_url(remote_tag, asset_name), package_path)
            print(f" {GRAY}↓ Downloading SHA256SUMS...{RESET}")
            _download_file(_release_download_url(remote_tag, "SHA256SUMS"), sha256sums_path)
        except (OSError, subprocess.SubprocessError) as e:
            print(f" {RED}❌ Failed to download package update: {e}{RESET}")
            return

        if not _verify_release_checksum(package_path, sha256sums_path):
            return

        command = get_package_upgrade_argv(package_path)
        if not command:
            print(f" {RED}❌ Unsupported Linux distribution for package updates.{RESET}")
            return

        print(f" {GRAY}Running package upgrade:{RESET} {BOLD}{' '.join(command)}{RESET}")
        try:
            process = subprocess.run(command)
        except (OSError, subprocess.SubprocessError) as e:
            print(f" {RED}❌ Package upgrade failed: {e}{RESET}")
            return

        if process.returncode == 0:
            print(f"\n {GREEN}✨ Topo has been successfully updated to {remote_tag}!{RESET}")
            print(
                f" {GRAY}If your shell still uses an old command path, run:{RESET} {BOLD}hash -r{RESET}"
            )
        else:
            print(f"\n {RED}❌ Package upgrade failed with exit code {process.returncode}{RESET}")


def run_update():
    """Updates topo from the latest GitHub Release when its tag is newer."""

    # 1. Get current local version
    # Since we are running from src/manage/update.py,
    # the VERSION file should be in the root of the installation (~/.topo/VERSION)
    local_version = _read_local_version()

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

    if get_install_source() == PACKAGE_INSTALL:
        _run_package_update(local_version, remote_tag)
        return

    # Refuse any tag that isn't a plain version-ish token. _parse_version already
    # proved it parses, but the raw tag goes into a URL and is handed to the
    # installer, so reject anything with shell metacharacters or whitespace.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", remote_tag):
        print(f" {RED}❌ Refusing unsafe release tag: {remote_tag!r}{RESET}")
        return

    print(f" {YELLOW}⬆️  New version available: {remote_tag}{RESET}")
    print(f" {GRAY}Updating Topo from v{local_version} to {remote_tag}...{RESET}\n")

    # 4. Download the release installer, then run it via `bash -s` stdin.
    # No shell=True and no string interpolation into a command line: the tag is
    # passed as a separate argv element, so a crafted tag cannot inject commands.
    script_url = f"https://raw.githubusercontent.com/Jesencloud/Topo/{remote_tag}/install.sh"
    try:
        script = subprocess.check_output(["curl", "-fsSL", script_url], text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"\n {RED}❌ Failed to download installer: {e}{RESET}")
        return

    # Sanity-check the payload before piping it into `bash`. The tag-pinned URL
    # over HTTPS already fixes the content for an untampered repo; this refuses
    # obviously-wrong bodies (CDN/error pages, truncated or empty downloads).
    if not script.lstrip().startswith("#!"):
        print(f"\n {RED}❌ Downloaded installer is not a valid script; aborting update.{RESET}")
        return

    try:
        process = subprocess.run(
            ["bash", "-s", "--", "--minimal", "--version", remote_tag],
            input=script,
            text=True,
        )
        if process.returncode == 0:
            print(f"\n {GREEN}✨ Topo has been successfully updated to {remote_tag}!{RESET}")
        else:
            print(f"\n {RED}❌ Update failed with exit code {process.returncode}{RESET}")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"\n {RED}❌ Error during update: {e}{RESET}")
