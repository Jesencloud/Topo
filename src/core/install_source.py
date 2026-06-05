from pathlib import Path

from .system import get_os_id

INSTALL_SOURCE_MARKER = ".topo-install-source"
SCRIPT_INSTALL = "script"
PACKAGE_INSTALL = "package"
APT_OS_IDS = {
    "debian",
    "ubuntu",
    "linuxmint",
    "pop",
    "elementary",
    "zorin",
    "kali",
}
DNF_OS_IDS = {
    "fedora",
    "rhel",
    "centos",
    "rocky",
    "almalinux",
    "ol",
    "amzn",
}


def get_install_root() -> Path:
    """Return the Topo application root for both script and package installs."""
    return Path(__file__).parent.parent.parent


def get_install_source() -> str:
    """Return how this Topo copy was installed.

    Older script installs do not have the marker, so they are treated as script
    installs for backward compatibility.
    """
    marker = get_install_root() / INSTALL_SOURCE_MARKER
    try:
        value = marker.read_text().strip().lower()
    except OSError:
        return SCRIPT_INSTALL
    if value == PACKAGE_INSTALL:
        return PACKAGE_INSTALL
    return SCRIPT_INSTALL


def get_package_manager_commands(action: str, os_id: str | None = None) -> list[str]:
    """Return distro-appropriate package-manager commands for package installs."""
    if action == "update":
        apt_command = "sudo apt upgrade topo"
        dnf_command = "sudo dnf upgrade topo"
    elif action == "remove":
        apt_command = "sudo apt remove topo"
        dnf_command = "sudo dnf remove topo"
    else:
        raise ValueError(f"Unsupported package-manager action: {action}")

    distro = (os_id or get_os_id()).lower()
    if distro in APT_OS_IDS:
        return [apt_command]
    if distro in DNF_OS_IDS:
        return [dnf_command]
    return [apt_command, dnf_command]
