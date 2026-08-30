import json
import re
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from packaging.version import InvalidVersion, Version

from ..core.constants import BOLD, FAIL, GRAY, GREEN, OK, RESET, TOPO_VERSION
from ..core.install_source import (
    PACKAGE_INSTALL,
    get_install_source,
    get_package_asset_name,
    get_package_upgrade_argv,
)
from ..core.system import PACKAGE_TRANSACTION_TIMEOUT

RELEASE_KEY_ASSET_NAME = "topo-release-public.asc"
RELEASE_SIGNATURE_ASSET_NAME = "SHA256SUMS.asc"
TOPO_RELEASE_KEY_FINGERPRINT = "4B35C17CF8E663732726A99F50086DB998B4D883"


def _parse_version(version_text: str) -> Version | None:
    try:
        return Version(version_text.strip().lstrip("vV"))
    except InvalidVersion:
        return None


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
            # Nothing here is guaranteed to be UTF-8: a captive portal or a
            # corporate proxy answers with its own Latin-1 error page rather than
            # with GitHub's JSON. Strict decoding would raise UnicodeDecodeError,
            # a ValueError that the except tuple below cannot catch, so an update
            # check behind a bad proxy ended in a traceback. Replacing the bad
            # bytes lets json.loads fail instead, which *is* handled -- and the
            # redirect fallback right below gets its turn.
            errors="replace",
            timeout=15,
        )
        tag = json.loads(data).get("tag_name", "")
        if isinstance(tag, str) and tag.strip():
            return tag.strip()
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        pass

    try:
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
            # Same reason. A tag carrying U+FFFD fails the version parse and the
            # `[A-Za-z0-9._+-]` tag check in run_update(), so a mangled redirect
            # is refused as an invalid tag rather than crashing the updater.
            errors="replace",
            timeout=15,
        )
        return latest_redirect_url.rstrip("/").rsplit("/", 1)[-1].split("?", 1)[0].strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _release_download_url(tag: str, asset_name: str) -> str:
    return f"https://github.com/Jesencloud/Topo/releases/download/{tag}/{asset_name}"


def _download_file(url: str, destination: Path, timeout: int = 60, attempts: int = 4) -> None:
    attempts = max(1, attempts)
    partial = destination.with_name(f"{destination.name}.part")
    last_error: OSError | subprocess.SubprocessError | None = None

    for attempt in range(1, attempts + 1):
        try:
            partial.unlink(missing_ok=True)
            argv = [
                "curl",
                "-fsSL",
                "--retry",
                "2",
                "--retry-delay",
                "1",
                "--retry-connrefused",
                "-A",
                "topo-updater",
                url,
                "-o",
                str(partial),
            ]
            result = subprocess.run(
                argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                # curl quotes the URL and the output path back in its diagnostics,
                # and a temp dir under a non-UTF-8 $TMPDIR puts undecodable bytes
                # in there. Strict decoding would turn a failed download -- the
                # case this retry loop exists for -- into an uncaught
                # UnicodeDecodeError that escapes both this except and the one in
                # _run_package_update().
                errors="replace",
                timeout=timeout,
            )
            if result.returncode == 0:
                partial.replace(destination)
                return
            last_error = subprocess.CalledProcessError(
                result.returncode,
                argv,
                stderr=result.stderr,
            )
        except (OSError, subprocess.SubprocessError) as e:
            last_error = e

        partial.unlink(missing_ok=True)
        if attempt < attempts:
            print(f" {GRAY}Download interrupted, retrying ({attempt + 1}/{attempts})...{RESET}")

    if last_error is not None:
        raise last_error


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


def _subprocess_stderr_tail(error: Any) -> str:
    stderr = getattr(error, "stderr", None)
    if not isinstance(stderr, str):
        return ""
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _normalized_gpg_fingerprint(value: str) -> str | None:
    fingerprint = value.upper()
    if re.fullmatch(r"[0-9A-F]{40}", fingerprint):
        return fingerprint
    return None


def _release_signature_status_matches(status_output: str) -> bool:
    for line in status_output.splitlines():
        payload = line.partition("[GNUPG:] ")[2]
        if not payload:
            continue
        fields = payload.split()
        if len(fields) < 2 or fields[0] != "VALIDSIG":
            continue

        signing_fingerprint = _normalized_gpg_fingerprint(fields[1])
        primary_fingerprint = _normalized_gpg_fingerprint(fields[-1])
        if primary_fingerprint == TOPO_RELEASE_KEY_FINGERPRINT:
            return True
        if primary_fingerprint is None and signing_fingerprint == TOPO_RELEASE_KEY_FINGERPRINT:
            return True
    return False


def _verify_release_signature(
    sha256sums_path: Path, signature_path: Path, public_key_path: Path
) -> bool:
    if not shutil.which("gpg"):
        print(f" {FAIL} gpg tool not found in system PATH. Refusing unverified update.")
        print(
            f" {GRAY}Signature verification requires gnupg. Install 'gnupg' package or update manually.{RESET}"
        )
        return False

    gpg_home = sha256sums_path.parent / "gnupg"
    try:
        gpg_home.mkdir(mode=0o700, exist_ok=True)
        gpg_home.chmod(0o700)
    except OSError as e:
        print(f" {FAIL} Failed to prepare temporary GPG home: {e}")
        return False

    base_argv = ["gpg", "--batch", "--homedir", str(gpg_home)]

    try:
        import_result = subprocess.run(
            [*base_argv, "--import", str(public_key_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            # gpg echoes a key's uid, which is free-form bytes chosen by whoever
            # made the key. Replacing the undecodable ones only ever degrades the
            # message _subprocess_stderr_tail() prints; it cannot affect the
            # verdict, which comes from the exit code.
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f" {FAIL} Failed to import Topo release key: {e}")
        return False

    if import_result.returncode != 0:
        print(f" {FAIL} Failed to import Topo release key.")
        if detail := _subprocess_stderr_tail(import_result):
            print(f" {GRAY}{detail}{RESET}")
        return False

    try:
        verify_result = subprocess.run(
            [
                *base_argv,
                "--status-fd=1",
                "--verify",
                str(signature_path),
                str(sha256sums_path),
            ],
            capture_output=True,
            text=True,
            # Cannot loosen the check: _release_signature_status_matches() accepts
            # only a `[GNUPG:] VALIDSIG` line whose fingerprint fullmatches
            # [0-9A-F]{40}, and U+FFFD is neither of those. So replacement can
            # only ever turn a pass into a refusal, never the other way round --
            # while strict decoding would crash on a uid gpg cannot decode.
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f" {FAIL} Failed to verify SHA256SUMS signature: {e}")
        return False

    if verify_result.returncode != 0 or not _release_signature_status_matches(verify_result.stdout):
        print(f" {FAIL} SHA256SUMS signature verification failed.")
        if detail := _subprocess_stderr_tail(verify_result):
            print(f" {GRAY}{detail}{RESET}")
        return False

    print(f" {OK} Verified SHA256SUMS signature with Topo release key")
    return True


def _verify_release_checksum(package_path: Path, sha256sums_path: Path) -> bool:
    expected = _expected_sha256(sha256sums_path, package_path.name)
    if not expected:
        print(f" {FAIL} SHA256SUMS does not list {package_path.name}.")
        return False
    actual = _file_sha256(package_path)
    if actual != expected:
        print(f" {FAIL} Checksum mismatch for {package_path.name}.")
        print(f" {GRAY}Expected: {expected}{RESET}")
        print(f" {GRAY}Actual:   {actual}{RESET}")
        return False
    print(f" {OK} Verified SHA256 for {package_path.name}")
    return True


def _run_package_update(local_version: str, remote_tag: str) -> bool:
    asset_name = get_package_asset_name(remote_tag)
    if not asset_name:
        # Two ways to get here, and the message names both: an unlisted distro,
        # or a machine topo builds no package for (riscv64, armv7l, i686).
        print(f" {FAIL} No Topo package for this distribution or architecture.")
        return False

    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", remote_tag):
        print(f" {FAIL} Refusing unsafe release tag: {remote_tag!r}")
        return False

    print(f" {GREEN}✨ New package available: {remote_tag}{RESET}")
    print(f" {GRAY}Updating Topo from v{local_version} to {remote_tag}...{RESET}\n")

    with TemporaryDirectory(prefix="topo-update-") as temp_dir:
        download_dir = Path(temp_dir)
        package_path = download_dir / asset_name
        sha256sums_path = download_dir / "SHA256SUMS"
        signature_path = download_dir / RELEASE_SIGNATURE_ASSET_NAME
        public_key_path = download_dir / RELEASE_KEY_ASSET_NAME
        gpg_available = shutil.which("gpg") is not None

        try:
            print(f" {GRAY}↓ Downloading {asset_name}...{RESET}")
            _download_file(_release_download_url(remote_tag, asset_name), package_path)
            print(f" {GRAY}↓ Downloading SHA256SUMS...{RESET}")
            _download_file(_release_download_url(remote_tag, "SHA256SUMS"), sha256sums_path)
            if gpg_available:
                print(f" {GRAY}↓ Downloading {RELEASE_SIGNATURE_ASSET_NAME}...{RESET}")
                _download_file(
                    _release_download_url(remote_tag, RELEASE_SIGNATURE_ASSET_NAME), signature_path
                )
                print(f" {GRAY}↓ Downloading {RELEASE_KEY_ASSET_NAME}...{RESET}")
                _download_file(
                    _release_download_url(remote_tag, RELEASE_KEY_ASSET_NAME), public_key_path
                )
        except (OSError, subprocess.SubprocessError) as e:
            print(f" {FAIL} Failed to download package update: {e}")
            if detail := _subprocess_stderr_tail(e):
                print(f" {GRAY}{detail}{RESET}")
            return False

        if not _verify_release_signature(sha256sums_path, signature_path, public_key_path):
            return False

        if not _verify_release_checksum(package_path, sha256sums_path):
            return False

        command = get_package_upgrade_argv(package_path)
        if not command:
            print(f" {FAIL} Unsupported Linux distribution for package updates.")
            return False

        print(f" {GRAY}Running package upgrade:{RESET} {BOLD}{' '.join(command)}{RESET}")
        try:
            # Same no-deadline rule as the removals: this is dpkg or rpm unpacking
            # and configuring a package, and a SIGKILL in the middle of it leaves
            # topo itself half-installed.
            process = subprocess.run(command, timeout=PACKAGE_TRANSACTION_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            print(f" {FAIL} Package upgrade failed: {e}")
            return False

        if process.returncode == 0:
            print(f"\n {GREEN}✨ Topo has been successfully updated to {remote_tag}!{RESET}")
            print(
                f" {GRAY}If your shell still uses an old command path, run:{RESET} {BOLD}hash -r{RESET}"
            )
            return True
        print(f"\n {FAIL} Package upgrade failed with exit code {process.returncode}")
        return False


def run_update() -> bool:
    """Update topo from the latest GitHub Release; False when the update did not happen.

    "Already up to date" and "local is newer" are successes -- the caller asked
    for the newest version and has it. Everything printed with ✗ is a failure,
    including a refused signature: the whole point of refusing an unverified
    update is that the caller must be able to tell.
    """

    # 1. Get current local version
    # Read once by core.constants from the VERSION file at the install root, so
    # the updater cannot disagree with `topo --version` about what is installed.
    # An unreadable VERSION arrives here as UNKNOWN_VERSION, which does not parse
    # as a version and so stops at the "Invalid local version" check below --
    # this used to fall back to 0.0.0, i.e. reinstall against any remote tag.
    local_version = TOPO_VERSION

    print(f" {OK} Checking for updates... (Local: v{local_version})")

    # 2. Fetch latest stable release tag
    try:
        remote_tag = _fetch_latest_release_tag()
    except (OSError, subprocess.SubprocessError) as e:
        print(f" {FAIL} Failed to check latest release: {e}")
        return False

    # 3. Compare and act
    #
    # An empty tag is not a bad tag: _fetch_latest_release_tag() swallows every
    # curl failure and returns "". Reporting that as `Invalid release tag: ''`
    # blamed the release for a missing curl or a dropped network, so separate the
    # two. curl is checked here rather than up front because this is the first
    # thing that needs it -- if it is missing, the fetch has already failed.
    if not remote_tag:
        if shutil.which("curl"):
            print(f" {FAIL} Could not determine the latest release version.")
            print(
                f" {GRAY}Check your network connection or GitHub availability, then retry.{RESET}"
            )
        else:
            print(f" {FAIL} curl not found in system PATH. Cannot check for updates.")
            print(f" {GRAY}Install the 'curl' package, or update manually.{RESET}")
        return False

    local_parsed = _parse_version(local_version)
    remote_parsed = _parse_version(remote_tag)
    if remote_parsed is None:
        print(f" {FAIL} Invalid release tag: {remote_tag!r}")
        return False
    if local_parsed is None:
        print(f" {FAIL} Invalid local version: {local_version!r}")
        return False
    if remote_parsed == local_parsed:
        print(f" {OK} Topo is already up to date! (v{local_version})")
        return True
    if remote_parsed < local_parsed:
        print(
            f" {OK} Local Topo is newer than remote. "
            f"(local: v{local_version}, remote: {remote_tag})"
        )
        return True

    if get_install_source() == PACKAGE_INSTALL:
        return _run_package_update(local_version, remote_tag)

    # Refuse any tag that isn't a plain version-ish token. _parse_version already
    # proved it parses, but the raw tag goes into a URL and is handed to the
    # installer, so reject anything with shell metacharacters or whitespace.
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", remote_tag):
        print(f" {FAIL} Refusing unsafe release tag: {remote_tag!r}")
        return False

    print(f" {GREEN}✨ New version available: {remote_tag}{RESET}")
    print(f" {GRAY}Updating Topo from v{local_version} to {remote_tag}...{RESET}\n")

    # 4. Verify release signature and SHA256 checksum for install.sh before execution
    with TemporaryDirectory(prefix="topo_script_update_") as td:
        tmp_dir = Path(td)
        sums_path = tmp_dir / "SHA256SUMS"
        sig_path = tmp_dir / "SHA256SUMS.asc"

        sums_url = f"https://github.com/Jesencloud/Topo/releases/download/{remote_tag}/SHA256SUMS"
        sig_url = (
            f"https://github.com/Jesencloud/Topo/releases/download/{remote_tag}/SHA256SUMS.asc"
        )

        try:
            _download_file(sums_url, sums_path)
            _download_file(sig_url, sig_path)
            key_path = tmp_dir / RELEASE_KEY_ASSET_NAME
            _download_file(
                f"https://github.com/Jesencloud/Topo/releases/download/{remote_tag}/{RELEASE_KEY_ASSET_NAME}",
                key_path,
            )

            if not _verify_release_signature(sums_path, sig_path, key_path):
                print(f"\n {FAIL} Release signature verification failed. Aborting script update.")
                return False

            expected_sha = _expected_sha256(sums_path, "install.sh")

            if expected_sha is None:
                print(
                    f"\n {FAIL} install.sh is not listed in the signed SHA256SUMS; aborting update."
                )
                return False

            script_url = (
                f"https://raw.githubusercontent.com/Jesencloud/Topo/{remote_tag}/install.sh"
            )
            raw_bytes = subprocess.check_output(["curl", "-fsSL", script_url], timeout=30)
            actual_sha = sha256(raw_bytes).hexdigest()

            if actual_sha.lower() != expected_sha.lower():
                print(f"\n {FAIL} SHA256 checksum mismatch for install.sh; aborting update.")
                return False

            script = raw_bytes.decode("utf-8", errors="strict")
        except (OSError, UnicodeDecodeError, subprocess.SubprocessError) as e:
            print(f"\n {FAIL} Failed to download signature/installer assets: {e}")
            return False

    if not script.lstrip().startswith("#!"):
        print(f"\n {FAIL} Downloaded installer is not a valid script; aborting update.")
        return False

    try:
        process = subprocess.run(
            ["bash", "-s", "--", "--minimal", "--version", remote_tag],
            input=script,
            text=True,
        )
        if process.returncode == 0:
            print(f"\n {GREEN}✨ Topo has been successfully updated to {remote_tag}!{RESET}")
            return True
        print(f"\n {FAIL} Update failed with exit code {process.returncode}")
    except (OSError, subprocess.SubprocessError) as e:
        print(f"\n {FAIL} Error during update: {e}")
    return False
