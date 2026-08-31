"""The one distro matrix: a row per package manager topo knows how to drive.

Before this module the same knowledge was spelled out five times, and the copies
had drifted into giving different answers on the same machine:

* `install_source` knew seven apt ids, seven dnf ids and four zypper ids, and no
  pacman at all -- so on Arch `topo update` and `topo remove` said "Unsupported
  Linux distribution" while `topo clean` and `topo uninstall` worked fine.
* `heavy_cache` knew three ids per manager (`fedora`/`rhel`/`centos`,
  `ubuntu`/`debian`, `arch`) and no zypper -- so `topo clean` skipped the
  package cache entirely on openSUSE, and on Linux Mint it only worked by
  accident, through the PATH fallback.
* `clean.system` and `uninstall` each rebuilt the tool list by hand, four call
  sites repeating `"dnf5" if shutil.which("dnf5") else "dnf"`.
* `doctor` probed a fourth list -- `apt` and `dpkg` rather than the `apt-get`
  and `dpkg-query` topo actually runs, `dnf` rather than dnf5, no pacman, and
  neither of the two tools an update really needs (curl, gpg).

Two lookups, because there are genuinely two questions:

* `detect_package_manager()` -- "which manager owns this machine": exact id,
  then the ID_LIKE family, then a PATH probe for distros nobody listed.
* `find_package_manager()` -- "which release asset may I hand this machine":
  exact id only. An unrecognised id must fail safe here; a Fedora box with the
  dpkg tools installed for `alien` must never be handed a .deb.
"""

import shutil
from dataclasses import dataclass

from .constants import AppType
from .system import get_os_id, is_os_family


@dataclass(frozen=True)
class PackageManager:
    """Everything topo needs to recognise and drive one package manager."""

    key: str
    # Also the app_type uninstall stores on every package it finds, and the word
    # clean prints ("Cleaned APT cache"). One label, so a rename cannot make the
    # scanner and the remover disagree about what to call the same package -- and
    # an AppType rather than a str, so the four rows here and uninstall's own
    # dispatch spell it from the same definition.
    label: AppType
    os_ids: frozenset[str]
    # ID_LIKE tokens, for the derivatives nobody enumerates.
    families: tuple[str, ...]
    # What reads the installed-package database (uninstall's scanners).
    query_tool: str
    # What performs privileged operations, newest generation first: dnf5 before
    # dnf, because Fedora 41+ ships `dnf` only as a compat symlink.
    admin_tools: tuple[str, ...]
    # Argv tails, appended to the resolved admin tool.
    cache_clean_args: tuple[str, ...]
    orphan_removal: bool = True
    # Which release asset fits this machine; None means topo publishes none.
    package_format: str | None = None
    # Argv for topo's own package, spelled out in full because these run through
    # sudo and are printed for the user to read. Empty means "not supported".
    topo_upgrade_args: tuple[str, ...] = ()
    topo_remove_args: tuple[str, ...] = ()


APT = PackageManager(
    key="apt",
    label=AppType.APT,
    os_ids=frozenset({"debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin", "kali"}),
    families=("debian",),
    query_tool="dpkg-query",
    admin_tools=("apt-get",),
    cache_clean_args=("clean",),
    package_format="deb",
    # `apt install ./x.deb` rather than apt-get: apt resolves a local file's
    # dependencies from the configured repositories.
    topo_upgrade_args=("apt", "install", "-y"),
    topo_remove_args=("apt", "remove", "-y", "topo"),
)

DNF = PackageManager(
    key="dnf",
    label=AppType.DNF,
    os_ids=frozenset({"fedora", "rhel", "centos", "rocky", "almalinux", "ol", "amzn"}),
    families=("fedora", "rhel"),
    query_tool="rpm",
    admin_tools=("dnf5", "dnf"),
    # `packages` alone had nothing to delete on a default Fedora: keepcache is
    # false, so a downloaded rpm is gone the moment its transaction finishes,
    # and what actually fills /var/cache/libdnf5 is metadata. `dbcache` adds the
    # solv/ indexes libdnf5 *generates* from that metadata -- man dnf5-clean:
    # "forces DNF5 to regenerate the cache files", locally, with no download --
    # so this frees the regenerable half and still leaves repodata/ alone.
    # Deliberately not `all`, and not `metadata`: those make the next dnf
    # command re-download every repository's metadata, which is the cost 6f0a8be
    # removed on purpose.
    cache_clean_args=("clean", "packages", "dbcache"),
    package_format="rpm",
    # Deliberately the unversioned `dnf`, not the resolved dnf5: these strings go
    # into the update/remove transcript the CI smoke tests grep for, and dnf is
    # the name that exists on every rpm release topo ships for.
    topo_upgrade_args=("dnf", "upgrade", "-y"),
    topo_remove_args=("dnf", "remove", "-y", "topo"),
)

ZYPPER = PackageManager(
    key="zypper",
    label=AppType.ZYPPER,
    os_ids=frozenset({"opensuse", "opensuse-leap", "opensuse-tumbleweed", "sles"}),
    families=("suse",),
    query_tool="rpm",
    admin_tools=("zypper",),
    cache_clean_args=("--non-interactive", "clean"),
    # `zypper remove --clean-deps` needs the packages to remove; there is no
    # unprivileged "list the orphans" equivalent of `apt-get autoremove` or
    # `pacman -Qtdq`, so clean has nothing safe to offer here.
    orphan_removal=False,
    package_format="rpm",
    topo_upgrade_args=("zypper", "--non-interactive", "install", "--allow-unsigned-rpm"),
    topo_remove_args=("zypper", "--non-interactive", "remove", "topo"),
)

PACMAN = PackageManager(
    key="pacman",
    label=AppType.PACMAN,
    os_ids=frozenset({"arch", "manjaro", "endeavouros"}),
    families=("arch",),
    query_tool="pacman",
    admin_tools=("pacman",),
    cache_clean_args=("-Sc", "--noconfirm"),
    # packaging/build-linux-packages.sh builds a .deb and an .rpm and nothing
    # else, so there is no asset to download: `topo update` correctly keeps
    # saying it cannot update a pacman install. Removal still works, for a
    # third-party PKGBUILD that left the package marker behind.
    package_format=None,
    topo_remove_args=("pacman", "-Rns", "--noconfirm", "topo"),
)

# Probe order for the PATH fallback below.
PACKAGE_MANAGERS: tuple[PackageManager, ...] = (APT, DNF, ZYPPER, PACMAN)

# Every tool that can answer "what packages are installed", deduplicated: rpm
# serves both DNF and Zypper.
PACKAGE_QUERY_TOOLS: tuple[str, ...] = tuple(
    dict.fromkeys(manager.query_tool for manager in PACKAGE_MANAGERS)
)


def find_package_manager(os_id: str | None = None) -> PackageManager | None:
    """The manager this distro id names, or None -- exact ids only.

    Used where the answer decides what gets downloaded and installed, so an id
    nobody listed has to come out as None rather than as a guess.
    """
    distro = (os_id or get_os_id()).lower()
    for manager in PACKAGE_MANAGERS:
        if distro in manager.os_ids:
            return manager
    return None


def detect_package_manager(os_id: str | None = None) -> PackageManager | None:
    """The manager that owns this machine: exact id, ID_LIKE family, then PATH.

    The family step reads /etc/os-release, so pass an os_id only when it is this
    machine's (clean does, to keep one os-release read per task); use
    find_package_manager() for a host-independent answer.

    The PATH probe is skipped for os_id "unknown", which is what get_os_id()
    reports when /etc/os-release is missing or unparsable. A machine that will
    not say what it is gets no guess from the tools that happen to be installed
    -- a container with `alien`'s dpkg in it is not a Debian box. Tests pass
    "unknown" for the same reason: it is the one id whose answer cannot depend on
    the developer's own machine.
    """
    distro = (os_id or get_os_id()).lower()
    if manager := find_package_manager(distro):
        return manager
    for manager in PACKAGE_MANAGERS:
        if any(is_os_family(family) for family in manager.families):
            return manager
    if distro != "unknown":
        for manager in PACKAGE_MANAGERS:
            if any(shutil.which(tool) for tool in manager.admin_tools):
                return manager
    return None


def get_rpm_family_manager() -> PackageManager:
    """Which manager owns the rpm database here: Zypper on the suse family.

    openSUSE and SLES are rpm distros without dnf, so labelling every rpm "DNF"
    sent their removals into a `dnf remove` that is not installed. The family is
    asked from os-release rather than from PATH: a Fedora box may well have
    zypper lying around, and the question is which manager owns the database.
    """
    return ZYPPER if is_os_family("suse") else DNF


def resolve_admin_tool(manager: PackageManager) -> str:
    """The admin binary to invoke, preferring the newest generation installed.

    Returns the canonical (oldest) name when none of them is installed, so a
    caller can `shutil.which()` the result to decide whether to run at all.
    """
    for candidate in manager.admin_tools:
        if shutil.which(candidate):
            return candidate
    return manager.admin_tools[-1]
