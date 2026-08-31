import os
import platform
from pathlib import Path

from .package_manager import find_package_manager

INSTALL_SOURCE_MARKER = ".topo-install-source"
SCRIPT_INSTALL = "script"
PACKAGE_INSTALL = "package"

# The only architectures a package is built for, spelled the way each packaging
# convention spells them. `platform.machine()` values on the left, so amd64 and
# arm64 are in here for the platforms that report those names. Same whitelist
# shape as _ENGINE_BY_ARCH in src/core/engine.py, and for the same reason: an
# unlisted machine has to come out as "no asset", not as amd64.
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
        # The marker is written by the packaging scripts, but it sits in a
        # user-writable tree: a mangled byte must make this fall back to
        # SCRIPT_INSTALL, not raise UnicodeDecodeError past `except OSError`.
        value = marker.read_text(errors="replace").strip().lower()
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

    Architecture is just as strict, for the same reason. topo builds packages for
    two machines; on riscv64 or armv7l there is nothing to download, and naming
    the amd64 file anyway turned an honest "no package for this machine" into
    either a 404 or -- on a machine whose package manager takes foreign
    architectures -- a download that installs an engine the kernel refuses to
    exec.

    The `-1` in both names is the package revision that
    packaging/build-linux-packages.sh passes as `--iteration 1`; it is part of
    the published filename, so it is part of the name computed here. The two
    spellings differ because the conventions do: Debian separates the revision
    with a hyphen inside the version field (name_upstream-revision_arch.deb),
    rpm gives it its own field (name-upstream-release.arch.rpm).
    """
    manager = find_package_manager(os_id)
    if manager is None:
        return None
    current_machine = _normalize_machine(machine)
    package_version = version.strip().lstrip("vV")
    if manager.package_format == "deb":
        deb_arch = DEB_ARCH_BY_MACHINE.get(current_machine)
        return None if deb_arch is None else f"topo_{package_version}-1_{deb_arch}.deb"
    if manager.package_format == "rpm":
        rpm_arch = RPM_ARCH_BY_MACHINE.get(current_machine)
        return None if rpm_arch is None else f"topo-{package_version}-1.{rpm_arch}.rpm"
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
