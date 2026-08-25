import os
import platform
from pathlib import Path

from .package_manager import find_package_manager

INSTALL_SOURCE_MARKER = ".topo-install-source"
SCRIPT_INSTALL = "script"
PACKAGE_INSTALL = "package"

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


def get_package_asset_name(
    version: str, os_id: str | None = None, machine: str | None = None
) -> str | None:
    """The release asset that fits this machine, or None when there is none.

    Strict id matching, through find_package_manager(): the file named here gets
    downloaded and handed to a package manager, so a distro nobody listed must
    come out as "no asset" rather than as a guess. Arch reaches this with a
    manager but no package_format -- topo publishes only .deb and .rpm.
    """
    manager = find_package_manager(os_id)
    if manager is None:
        return None
    current_machine = _normalize_machine(machine)
    package_version = version.strip().lstrip("vV")
    if manager.package_format == "deb":
        deb_arch = DEB_ARCH_BY_MACHINE.get(current_machine, "amd64")
        return f"topo_{package_version}_{deb_arch}.deb"
    if manager.package_format == "rpm":
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
    manager = find_package_manager(os_id)
    if manager is None or not manager.topo_upgrade_args:
        return None
    return [*_sudo_prefix(), *manager.topo_upgrade_args, str(package_path)]


def get_package_remove_argv(os_id: str | None = None) -> list[str] | None:
    manager = find_package_manager(os_id)
    if manager is None or not manager.topo_remove_args:
        return None
    return [*_sudo_prefix(), *manager.topo_remove_args]
