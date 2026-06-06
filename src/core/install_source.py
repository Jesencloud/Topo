import os
import platform
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

DEB_ARCH_BY_MACHINE = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}
RPM_ARCH_BY_MACHINE = {
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "aarch64": "aarch64",
    "arm64": "aarch64",
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


def _normalize_machine(machine: str | None = None) -> str:
    return (machine or platform.machine()).lower()


def get_package_manager(os_id: str | None = None) -> str | None:
    distro = (os_id or get_os_id()).lower()
    if distro in APT_OS_IDS:
        return "apt"
    if distro in DNF_OS_IDS:
        return "dnf"
    return None


def get_package_asset_name(
    version: str, os_id: str | None = None, machine: str | None = None
) -> str | None:
    package_manager = get_package_manager(os_id)
    current_machine = _normalize_machine(machine)
    package_version = version.strip().lstrip("vV")
    if package_manager == "apt":
        deb_arch = DEB_ARCH_BY_MACHINE.get(current_machine, "amd64")
        return f"topo_{package_version}_{deb_arch}.deb"
    if package_manager == "dnf":
        rpm_arch = RPM_ARCH_BY_MACHINE.get(current_machine, "x86_64")
        return f"topo-{package_version}-1.{rpm_arch}.rpm"
    return None


def _sudo_prefix() -> list[str]:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    return ["sudo"]


def get_package_upgrade_argv(
    package_path: str | Path, os_id: str | None = None
) -> list[str] | None:
    package_manager = get_package_manager(os_id)
    sudo = _sudo_prefix()
    if package_manager == "apt":
        return [*sudo, "apt", "install", "-y", str(package_path)]
    if package_manager == "dnf":
        return [*sudo, "dnf", "upgrade", "-y", str(package_path)]
    return None


def get_package_remove_argv(os_id: str | None = None) -> list[str] | None:
    package_manager = get_package_manager(os_id)
    sudo = _sudo_prefix()
    if package_manager == "apt":
        return [*sudo, "apt", "remove", "-y", "topo"]
    if package_manager == "dnf":
        return [*sudo, "dnf", "remove", "-y", "topo"]
    return None


def get_package_manager_commands(
    action: str, os_id: str | None = None, machine: str | None = None
) -> list[str]:
    """Return distro-appropriate package-manager commands for package installs."""
    if action == "update":
        current_machine = _normalize_machine(machine)
        deb_arch = DEB_ARCH_BY_MACHINE.get(current_machine, "amd64")
        rpm_arch = RPM_ARCH_BY_MACHINE.get(current_machine, "x86_64")
        apt_command = f"sudo apt install ./topo_xxx_{deb_arch}.deb"
        dnf_command = f"sudo dnf upgrade ./topo-xxx-1.{rpm_arch}.rpm"
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
