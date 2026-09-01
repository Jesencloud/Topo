"""What is installed on this machine, from the eight places that know.

Nothing here deletes anything. Every function answers one question -- "what apps
does this package manager report, and how big are they" -- and the whole module
has one entry point, :func:`discover_installed_apps`, which fans the eight
sources out across a thread pool and returns their records concatenated.

The eight: rpm, dpkg, pacman, Flatpak, Snap, npm's global tree, standalone CLI
tools under ``~/.local/bin`` or a dotted home directory, and the pre-scan that
asks the three native managers which of them owns each installed ``.desktop``
file (that last one is what turns a package id into the name a user recognises,
and what decides which packages count as user-facing at all).

Split out of ``UninstallManager``, where these sat between residue discovery and
process termination: reading how a residue path is found meant scrolling past
eight package managers that have nothing to do with it. They shared no state
with the rest of the class -- each one runs its own tool and builds its own
records through ``_app_record`` -- so they came out first, and the rest of the
class followed them into ``residue.py``, ``processes.py`` and ``removal.py``.

Two helpers here are also read by the modules downstream: :func:`app_text` by
``residue.py`` and :func:`strip_package_arch` by ``collateral.py``. Both are
about reading a package's own words, which is what this module does; the
classifier that consumes ``app_text`` on the other side
(``_requires_official_only_uninstall``) decides whether an app's data may be
touched at all, and that decision belongs next to the search it cancels.
"""

import contextlib
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TypedDict

from ..core import system
from ..core.constants import (
    RPM_QUERY_BATCH_SIZE,
    AppType,
)
from ..core.desktop_entry import get_desktop_name
from ..core.file_ops import bytes_to_human, get_size_fast, parse_size_to_bytes
from ..core.package_manager import get_rpm_family_manager

# dpkg-query -W reports every entry the status database knows about, installed or
# not, so the status pair and deb's own protection metadata have to come back with
# the size. ${Essential} is normalised to "yes"/"no"; ${Priority} can be empty.
_DPKG_QUERY_FORMAT = (
    "${db:Status-Abbrev}\t${binary:Package}\t${Essential}\t${Priority}\t${Installed-Size}\n"
)
# Status-Abbrev is want+status+error ("ii ", "rc "). Only the middle character
# says whether the files are on disk: `dpkg -r` without purge leaves a package at
# "rc" (config-files) with its old Installed-Size still recorded, so counting
# anything outside this set reports space that was freed long ago. Left out: n
# (not-installed), c (config-files) and H (half-installed, size unreliable).
_DPKG_UNPACKED_STATUS = frozenset("iUFWt")
# Where dpkg records the file list it writes on unpack; the list's mtime is the
# only install timestamp the status database offers.
_DPKG_INFO_DIR = Path("/var/lib/dpkg/info")
# The database every dpkg-query answer comes out of. Installing the dpkg tools on
# a non-deb distro creates it empty, and then `dpkg-query -S` can only ever reply
# "no path found matching pattern" -- once per batch, at a fork apiece.
_DPKG_STATUS_FILE = Path("/var/lib/dpkg/status")
# The rpm equivalent. rpm keeps a directory rather than one file, and which files
# are in it depends on the backend (rpmdb.sqlite today, Packages under bdb,
# Packages.db under ndb), so the check is "does it hold anything at all" instead
# of a filename. On Fedora this is a symlink to /usr/lib/sysimage/rpm, which
# stat() follows.
_RPM_DB_DIR = Path("/var/lib/rpm")


class _ScannedApp(TypedDict):
    """What every one of the seven scanners produces, by way of ``_app_record``.

    Required, because that factory is the only way a record is born and it fills
    all six: ``id`` is what the package manager is asked to remove, ``name`` is
    what the user reads (display text picked up from the environment -- a
    ``.desktop`` ``Name=`` field, a filename, an npm package name -- so it is
    untrusted), ``type`` is one of the ``AppType`` words and names the manager
    that owns it -- held as the plain string it spells, so a record from a scan
    and a record built by hand are the same shape -- ``size_bytes`` and
    ``size_str`` are the same number for sorting and for display, and
    ``install_time`` is 0 when nothing could be read rather than absent.
    """

    id: str
    name: str
    size_bytes: int
    size_str: str
    type: str
    install_time: int


class AppRecord(_ScannedApp, total=False):
    """A scanned app, plus the three fields later stages attach to some of them.

    These three were the argument for typing the record at all. They exist on a
    subset of the apps -- ``flatpak_scope`` only on Flatpaks whose scope column
    was not empty, ``install_dir`` only on standalone CLI tools,
    ``collateral_packages`` only on apps that reached the removal preview -- and
    before this class the only thing that said so was whether a call site reached
    for ``app["x"]`` or ``app.get("x")``. Those two spellings were split across
    the same keys (``app["id"]`` at 16 sites, ``app.get("id")`` at four), so the
    distinction they were supposedly drawing was not one a reader could trust.

    Split in two classes rather than written with ``NotRequired`` because the
    supported floor is Python 3.10, where that spelling does not exist yet;
    per-class ``total`` is how PEP 589 says the same thing.
    """

    flatpak_scope: str
    install_dir: Path
    collateral_packages: list[str]


class _DiscoveredTool(TypedDict):
    """One standalone CLI tool found on disk, before it is sized into a record.

    Two loops in ``_scan_standalone_cli_apps`` build these -- one over
    ``~/.local/bin``, one over the dotted directories in ``$HOME`` -- and a third
    loop reads them back. Untyped, all four fields had to be re-declared on the
    way out: two ``str()`` calls that could not convert anything, because the
    writer twenty lines up had put a ``str`` there, and two annotations
    (``bin_path: Path = tool["binary"]``) that only told the reader the same.
    """

    id: str
    name: str
    binary: Path
    install_dir: Path


def _has_deb_database() -> bool:
    """Whether dpkg-query has anything to answer from."""
    with contextlib.suppress(OSError):
        return _DPKG_STATUS_FILE.stat().st_size > 0
    return False


def _has_rpm_database() -> bool:
    """Whether rpm has anything to answer from.

    Asymmetric on purpose: this says False only when the database directory is
    missing or provably holds nothing, so a backend nobody here has heard of
    still counts as a database and an rpm distro never silently loses its whole
    package list. The case being excluded is a Debian box with `rpm` installed as
    an `alien` dependency, where the directory ships empty and `rpm -qa` is a
    60-second-timeout fork that can only answer nothing.
    """
    with contextlib.suppress(OSError):
        return any(entry.is_file() and entry.stat().st_size > 0 for entry in _RPM_DB_DIR.iterdir())
    return False


def app_text(app_id: str, app_name: str) -> str:
    """An app's id and display name as one lowercase string, for token matching."""
    return f"{app_id} {app_name}".lower()


def strip_package_arch(package_name: str) -> str:
    return package_name.split(":", 1)[0]


_SYSTEM_COMPONENT_TOKENS = frozenset(
    {
        "akmod",
        "cldr-emoji",
        "cinnamon",
        "dracut",
        "evolution-data-server",
        "firmware",
        "fontconfig",
        "gcc",
        "g++",
        "gcr",
        "geoclue",
        "glibc",
        "gnome-bluetooth",
        "gnome-control-center",
        "gnome-session",
        "gnome-settings-daemon",
        "gnome-shell",
        "gnome-software",
        "gnome-user-share",
        "grub",
        "ibus",
        "openjdk",
        "kernel",
        "kmod",
        "kwin",
        "langpack",
        "linux-headers",
        "linux-image",
        "llvm",
        "malcontent",
        "mesa",
        "mutter",
        "networkmanager",
        "nvidia-driver",
        "pipewire",
        "plasma",
        "pulseaudio",
        "qemu-common",
        "rust-std",
        "rygel",
        "selinux",
        "systemd",
        "tecla",
        "wayland",
        "wireplumber",
        "xdg-user-dirs",
        "xfce",
        "xorg",
    }
)


_SYSTEM_COMPONENT_EXACT_IDS = frozenset(
    {
        "baobab",
        "dconf",
        "decibels",
        "gdm",
        "gnome-boxes",
        "gnome-browser-connector",
        "gnome-calculator",
        "gnome-calendar",
        "gnome-characters",
        "gnome-clocks",
        "gnome-color-manager",
        "gnome-contacts",
        "gnome-connections",
        "gnome-disk-utility",
        "gnome-font-viewer",
        "gnome-initial-setup",
        "gnome-logs",
        "gnome-maps",
        "gnome-online-accounts",
        "gnome-system-monitor",
        "gnome-terminal",
        "gnome-text-editor",
        "gnome-tour",
        "gnome-tweaks",
        "gnome-weather",
        "gvfs",
        "libreoffice-core",
        "libreoffice-xsltfilter",
        "loupe",
        "mediawriter",
        "nautilus",
        "orca",
        "org.gnome.characters",
        "papers",
        "papers-previewer",
        "ptyxis",
        "showtime",
        "simple-scan",
        "eog",
        "evince",
        "file-roller",
        "gedit",
        "gnome-mahjongg",
        "gnome-mines",
        "gnome-sudoku",
        "gnome-system-log",
        "gnome-user-docs",
        "language-selector-gnome",
        "remmina",
        "seahorse",
        "software-properties-gtk",
        "totem",
        "ubuntu-desktop",
        "ubuntu-desktop-minimal",
        "ubuntu-docs",
        "ubuntu-drivers-common",
        "ubuntu-minimal",
        "ubuntu-release-upgrader-gtk",
        "ubuntu-session",
        "ubuntu-standard",
        "usb-creator-gtk",
        "xdg-desktop-portal-ubuntu",
        "xdg-desktop-portal",
        "xdg-desktop-portal-gnome",
        "xdg-desktop-portal-gtk",
        "xdg-desktop-portal-kde",
        "xdg-desktop-portal-wlr",
        "xdg-desktop-portal-lxqt",
        "xdg-user-dirs-gtk",
        "snapshot",
        "gnome-snapshot",
        "org.gnome.Snapshot",
        "org.gnome.snapshot",
        "cheese",
        "yelp",
    }
)


def _is_system_component(app_id: str, app_name: str) -> bool:
    text = app_text(app_id, app_name)
    app_id_lower = app_id.lower()
    if app_id_lower in _SYSTEM_COMPONENT_EXACT_IDS or any(
        token in text for token in _SYSTEM_COMPONENT_TOKENS
    ):
        return True

    # Generic System Heuristics for non-app packages:
    # Prevent non-GUI libraries, development headers, static archives from leaking
    sys_suffixes = (
        "-libs",
        "-devel",
        "-dev",
        "-static",
        "-headers",
        "-plugins",
        "-modules",
    )
    if app_id_lower.startswith("libreoffice"):
        return any(app_id_lower.endswith(s) for s in sys_suffixes)

    # A CJK font package is shipped as one 100 MB+ bundle (fonts-noto-cjk on
    # deb, google-noto-*-fonts on Fedora, noto-fonts-cjk on Arch) with no
    # .desktop file, so the "anything over 100 MB is a user app" fallback used
    # to offer it up for removal -- and removing it turns every Chinese,
    # Japanese and Korean glyph on the desktop into a box. Matching a whole
    # "fonts" segment protects all three naming styles while leaving font
    # applications alone: fontforge and font-manager have no such segment.
    if "fonts" in app_id_lower.replace("_", "-").split("-"):
        return True

    sys_prefixes = ("lib", "gsettings-", "desktop-file-", "shared-mime-", "ttf-", "otf-")
    return any(app_id_lower.endswith(s) for s in sys_suffixes) or any(
        app_id_lower.startswith(p) for p in sys_prefixes
    )


def _dpkg_install_time(*package_names: str) -> int:
    """When dpkg last wrote the package's file list, i.e. when it unpacked it.

    A `Multi-Arch: same` package keeps the architecture qualifier in the file
    name -- libacl1:amd64.list, not libacl1.list -- and more than half of the
    entries in /var/lib/dpkg/info on a stock ubuntu:24.04 look like that. The
    qualified name therefore has to be tried before the stripped one, or the
    timestamp stays 0 and the whole "most recently installed first" ordering
    collapses for those packages.
    """
    for name in package_names:
        list_file = _DPKG_INFO_DIR / f"{name}.list"
        if list_file.exists():
            with contextlib.suppress(OSError):
                return int(list_file.stat().st_mtime)
    return 0


def _app_record(
    app_id: str,
    name: str,
    size_bytes: int,
    size_str: str,
    app_type: AppType,
    install_time: int = 0,
) -> AppRecord:
    return {
        "id": app_id,
        "name": name,
        "size_bytes": size_bytes,
        "size_str": size_str,
        # The word, not the member: a record is a plain dict, and the tests
        # build theirs by hand, so ``type`` reads the same whichever it came
        # from. The annotation above is where the spelling is checked -- mypy
        # rejects a ninth package type invented at a call site.
        "type": app_type.value,
        "install_time": install_time,
    }


def _installed_desktop_files() -> list[Path]:
    """Every readable .desktop entry the system and the user have installed.

    Existence is re-checked after the glob: glob() still yields a dangling
    symlink, and rpm reports an unreadable path on stderr instead of stdout
    -- that drops a line from the batch and shifts every later name onto the
    wrong package. Dropping the unreadable entries here keeps the reply one
    line per queried path.
    """
    desktop_files: list[Path] = []
    for directory in (
        Path("/usr/share/applications"),
        Path.home() / ".local/share/applications",
    ):
        # An unreadable directory costs its own entries and nobody else's.
        # pathlib swallows a PermissionError while walking, but on the
        # supported floor it lets every other OSError through, and this
        # suppress used to sit around the whole function: one directory
        # failing threw away the other one's files too, along with every
        # package name already collected from them.
        with contextlib.suppress(OSError):
            if directory.exists():
                desktop_files.extend(
                    entry for entry in directory.glob("*.desktop") if entry.exists()
                )
    return desktop_files


def _rpm_desktop_owners(batch: list[Path]) -> list[tuple[str, Path]]:
    """Ask rpm which package owns each of these .desktop files.

    rpm answers positionally and prints its "not owned by any package" notice
    on stdout, so a reply of the wrong length means the answers no longer
    line up with the questions. Losing a few display names beats attaching
    them to the wrong package, so a mismatched batch is dropped whole.
    """
    result = system.run_command(
        ["rpm", "-qf", "--queryformat", "%{NAME}\n"] + [str(path) for path in batch],
        capture=True,
        timeout=60,
        env=system.C_LOCALE_ENV,
    )
    lines = result.stdout.splitlines()
    if len(lines) != len(batch):
        return []
    owners: list[tuple[str, Path]] = []
    for desktop_file, line in zip(batch, lines, strict=True):
        package = line.strip()
        # C_LOCALE_ENV above keeps this marker in English.
        if package and not package.startswith("file "):
            owners.append((package, desktop_file))
    return owners


def _dpkg_desktop_owners(batch: list[Path]) -> list[tuple[str, Path]]:
    """Ask dpkg which packages own each of these .desktop files.

    Every owner of a path shares one comma-separated line ("procps,
    libc6:amd64, bash: /usr/share/doc") and an owner may carry its own :arch,
    so the path is what follows the LAST ": " -- splitting on the first colon
    cuts libc6:amd64 in half and turns the rest into a package name that does
    not exist. One path can therefore come back with several owners, and all
    of them get the entry's display name.
    """
    result = system.run_command(
        ["dpkg-query", "-S", *[str(path) for path in batch]],
        capture=True,
        env=system.C_LOCALE_ENV,
    )
    owners: list[tuple[str, Path]] = []
    for line in result.stdout.splitlines():
        owners_field, separator, path_field = line.rpartition(": ")
        if not separator:
            continue
        desktop_file = Path(path_field.strip())
        for owner in owners_field.split(", "):
            package = strip_package_arch(owner.strip())
            if package:
                owners.append((package, desktop_file))
    return owners


def _pacman_desktop_owners(batch: list[Path]) -> list[tuple[str, Path]]:
    """Ask pacman which package owns each of these .desktop files.

    " is owned by " sits in pacman's gettext catalog, so the split below only
    holds under a C locale.
    """
    result = system.run_command(
        ["pacman", "-Qo", *[str(path) for path in batch]],
        capture=True,
        env=system.C_LOCALE_ENV,
    )
    owners: list[tuple[str, Path]] = []
    for line in result.stdout.splitlines():
        if " is owned by " not in line:
            continue
        queried_path, owner_field = line.split(" is owned by ", 1)
        owner_words = owner_field.split()
        if owner_words:
            owners.append((owner_words[0], Path(queried_path.strip())))
    return owners


def _pre_scan_package_desktop_names() -> tuple[set[str], dict[str, str]]:
    """Pre-scans native packages that provide desktop files and maps display names.

    There is no blanket try/except around this any more. The old one wrapped
    all 108 lines in `except (OSError, subprocess.SubprocessError,
    ValueError): pass`, which said "any distro tool may be absent or answer
    oddly" but could not actually catch that: system.run_command turns every
    OSError and SubprocessError of its own into a CommandResult with
    returncode 127, so no tool call in here has ever raised. What it did
    catch was the glob, now handled per directory where it happens, and what
    it stood ready to catch was any exception a future line grew -- silently,
    and by throwing away the whole pre-scan rather than the one answer.
    """
    user_app_packages: set[str] = set()
    package_desktop_names: dict[str, str] = {}
    desktop_files = _installed_desktop_files()
    if not desktop_files:
        return user_app_packages, package_desktop_names

    # One PATH search per tool, not one per batch: a desktop with a few
    # thousand .desktop files splits into many batches, and the answer
    # cannot change while the loop runs. The tools stay additive --
    # a deb box with rpm installed for `alien` must still reach the
    # dpkg branch, or every APT app falls out of the list -- so the
    # box that merely has the *tools* of another distro is excluded by
    # asking whether the database behind them holds anything.
    queries: list[Callable[[list[Path]], list[tuple[str, Path]]]] = []
    if shutil.which("rpm") and _has_rpm_database():
        queries.append(_rpm_desktop_owners)
    if shutil.which("dpkg-query") and _has_deb_database():
        queries.append(_dpkg_desktop_owners)
    if shutil.which("pacman"):
        queries.append(_pacman_desktop_owners)

    # The three tools disagree about output format, not about the question,
    # so each one only parses its own reply into (package, .desktop file)
    # pairs and the bookkeeping below is written once.
    for batch_start in range(0, len(desktop_files), RPM_QUERY_BATCH_SIZE):
        batch = desktop_files[batch_start : batch_start + RPM_QUERY_BATCH_SIZE]
        for query in queries:
            for package, desktop_file in query(batch):
                user_app_packages.add(package)
                # A display name is worth a file read only for a package that
                # has none yet. exists() is also what keeps a path a tool
                # echoed back from reaching open(): a NUL byte in it raises
                # ValueError, which get_desktop_name does not catch.
                if package not in package_desktop_names and desktop_file.exists():
                    display_name = get_desktop_name(desktop_file)
                    if display_name:
                        package_desktop_names[package] = display_name
    return user_app_packages, package_desktop_names


def _scan_rpm_packages(
    user_app_packages: set[str], package_desktop_names: dict[str, str]
) -> list[AppRecord]:
    if not shutil.which("rpm") or not _has_rpm_database():
        return []
    # openSUSE and SLES are rpm distros without dnf, so labelling everything
    # rpm reports "DNF" sent their removals into a `dnf remove` that is not
    # installed. get_rpm_family_manager() asks os-release once, and covers the
    # derivatives (ID_LIKE=suse) an exact id list would miss -- deliberately
    # not the strict find_package_manager(), which answers a different
    # question (which release asset to download, where an unrecognised id
    # must fail safe).
    app_type = get_rpm_family_manager().label
    apps = []
    try:
        res = system.run_command(
            ["rpm", "-qa", "--queryformat", "%{NAME}\t%{SIZE}\t%{INSTALLTIME}\n"],
            capture=True,
            timeout=60,
            env=system.C_LOCALE_ENV,
        )
        if res.ok:
            for line in res.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    app_id, size_bytes, install_time = (
                        parts[0],
                        int(parts[1]),
                        int(parts[2]),
                    )
                    if _is_system_component(app_id, app_id):
                        continue
                    if app_id in user_app_packages or size_bytes > 100 * 1024 * 1024:
                        display_name = package_desktop_names.get(app_id, app_id)
                        apps.append(
                            _app_record(
                                app_id,
                                display_name,
                                size_bytes,
                                bytes_to_human(size_bytes),
                                app_type,
                                install_time,
                            )
                        )
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return apps


def _scan_apt_packages(
    user_app_packages: set[str], package_desktop_names: dict[str, str]
) -> list[AppRecord]:
    if not shutil.which("dpkg-query") or not _has_deb_database():
        return []
    apps = []
    try:
        res = system.run_command(
            ["dpkg-query", "-W", f"-f={_DPKG_QUERY_FORMAT}"],
            capture=True,
            timeout=60,
            env=system.C_LOCALE_ENV,
        )
        if res.ok:
            for line in res.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) < 5:
                    continue
                status, raw_id, essential, priority, size_field = (
                    part.strip() for part in parts[:5]
                )
                if len(status) < 2 or status[1] not in _DPKG_UNPACKED_STATUS:
                    continue
                app_id = strip_package_arch(raw_id)
                try:
                    size_bytes = int(size_field) * 1024
                except ValueError:
                    continue
                # dpkg carries its own answer to "may this be removed", which
                # covers packages the hardcoded name lists were written for
                # Fedora and never matched (network-manager, nvidia-dkms-535).
                if essential == "yes" or priority in ("required", "important"):
                    continue
                if _is_system_component(app_id, app_id):
                    continue
                if app_id in user_app_packages or size_bytes > 100 * 1024 * 1024:
                    install_time = _dpkg_install_time(raw_id, app_id)
                    display_name = package_desktop_names.get(app_id, app_id)
                    apps.append(
                        _app_record(
                            app_id,
                            display_name,
                            size_bytes,
                            bytes_to_human(size_bytes),
                            AppType.APT,
                            install_time,
                        )
                    )
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return apps


def _scan_pacman_packages(
    user_app_packages: set[str], package_desktop_names: dict[str, str]
) -> list[AppRecord]:
    if not shutil.which("pacman"):
        return []
    apps = []
    try:
        # "Installed Size" is a translated field label with a localized
        # number, so the C locale is required to read it back.
        res = system.run_command(
            ["pacman", "-Qi"], capture=True, timeout=60, env=system.C_LOCALE_ENV
        )
        if res.ok:
            package: dict[str, str] = {}
            for line in [*res.stdout.splitlines(), ""]:
                if not line.strip():
                    app_id = package.get("Name", "").strip()
                    size_bytes = parse_size_to_bytes(package.get("Installed Size", ""))
                    if (
                        app_id
                        and not _is_system_component(app_id, app_id)
                        and (app_id in user_app_packages or size_bytes > 100 * 1024 * 1024)
                    ):
                        display_name = package_desktop_names.get(app_id, app_id)
                        apps.append(
                            _app_record(
                                app_id,
                                display_name,
                                size_bytes,
                                bytes_to_human(size_bytes),
                                AppType.PACMAN,
                            )
                        )
                    package = {}
                    continue
                if ":" in line:
                    key, value = line.split(":", 1)
                    package[key.strip()] = value.strip()
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return apps


def _scan_flatpak_apps() -> list[AppRecord]:
    if not shutil.which("flatpak"):
        return []
    apps = []
    try:
        # The size column is formatted for the locale ("1,2 GB" in de_DE),
        # which parse_size_to_bytes cannot read.
        res = system.run_command(
            ["flatpak", "list", "--app", "--columns=name,application,size,installation"],
            capture=True,
            timeout=60,
            env=system.C_LOCALE_ENV,
        )
        if res.ok:
            for line in res.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    app_name, app_id, size_str = parts[0], parts[1], parts[2]
                    # Which installation the app lives in decides who is
                    # allowed to remove it, so it is carried on the record
                    # instead of being asked for again at removal time --
                    # where the answer would arrive too late for the sudo
                    # session the screen takes before it enters raw mode. A
                    # flatpak that prints fewer columns than were asked for
                    # leaves this empty, which removal reads as "unknown".
                    scope = parts[3].strip() if len(parts) > 3 else ""
                    install_time = 0
                    try:
                        paths_to_check = [
                            Path(f"/var/lib/flatpak/app/{app_id}"),
                            Path.home() / f".local/share/flatpak/app/{app_id}",
                        ]
                        for p in paths_to_check:
                            if p.exists():
                                install_time = int(p.stat().st_mtime)
                                break
                    except OSError:
                        pass

                    id_lower = app_id.lower()
                    if "org.freedesktop" in id_lower or "org.gnome.platform" in id_lower:
                        continue
                    if _is_system_component(app_id, app_name):
                        continue

                    size_bytes = parse_size_to_bytes(size_str)
                    record = _app_record(
                        app_id, app_name, size_bytes, size_str, AppType.FLATPAK, install_time
                    )
                    if scope:
                        record["flatpak_scope"] = scope
                    apps.append(record)
    except (OSError, subprocess.SubprocessError):
        pass
    return apps


def _scan_snap_apps(package_desktop_names: dict[str, str]) -> list[AppRecord]:
    if not shutil.which("snap"):
        return []
    apps = []
    try:
        res = system.run_command(
            ["snap", "list"], capture=True, timeout=60, env=system.C_LOCALE_ENV
        )
        if res.ok:
            for line in res.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 2:
                    continue
                app_id = parts[0]
                revision = parts[2] if len(parts) >= 3 else ""
                if app_id in {"core", "core18", "core20", "core22", "core24", "snapd"}:
                    continue
                if _is_system_component(app_id, app_id):
                    continue

                size_bytes = 0
                size_str = "N/A"
                install_time = 0

                if revision:
                    snap_file = Path(f"/var/lib/snapd/snaps/{app_id}_{revision}.snap")
                    if snap_file.exists():
                        try:
                            size_bytes = snap_file.stat().st_size
                            size_str = bytes_to_human(size_bytes)
                            install_time = int(snap_file.stat().st_mtime)
                        except OSError:
                            pass

                if size_bytes == 0:
                    for mount_root in ["/snap", "/var/lib/snapd/snap"]:
                        snap_path = Path(f"{mount_root}/{app_id}/current")
                        if snap_path.exists():
                            try:
                                if install_time == 0:
                                    install_time = int(snap_path.stat().st_mtime)
                                res_size = system.run_command(
                                    ["du", "-sk", str(snap_path)],
                                    capture=True,
                                    timeout=5,
                                    env=system.C_LOCALE_ENV,
                                )
                                if res_size.ok and res_size.stdout:
                                    kb = int(res_size.stdout.split()[0])
                                    size_bytes = kb * 1024
                                    size_str = bytes_to_human(size_bytes)
                                    break
                            except (OSError, ValueError, IndexError):
                                pass

                display_name = package_desktop_names.get(app_id, app_id)
                apps.append(
                    _app_record(
                        app_id, display_name, size_bytes, size_str, AppType.SNAP, install_time
                    )
                )
    except (OSError, subprocess.SubprocessError):
        pass
    return apps


def _scan_npm_global_packages() -> list[AppRecord]:
    if not shutil.which("npm"):
        return []
    apps = []
    try:
        system_npm_pkgs = {"npm", "corepack", "yarn", "pnpm", "npx", "cnpm"}
        npm_roots = [
            Path.home() / ".npm-global" / "lib" / "node_modules",
            Path("/usr/lib/node_modules"),
            Path("/usr/local/lib/node_modules"),
        ]
        seen_npm_pkgs = set()
        for npm_modules_dir in npm_roots:
            if not npm_modules_dir.is_dir():
                continue
            try:
                for item in npm_modules_dir.iterdir():
                    pkg_dirs = []
                    if item.name.startswith("@") and item.is_dir():
                        with contextlib.suppress(OSError):
                            pkg_dirs.extend([sub for sub in item.iterdir() if sub.is_dir()])
                    elif item.is_dir():
                        pkg_dirs.append(item)

                    for pkg_path in pkg_dirs:
                        pkg_name = (
                            f"{item.name}/{pkg_path.name}"
                            if item.name.startswith("@")
                            else pkg_path.name
                        )
                        if pkg_name in seen_npm_pkgs:
                            continue
                        clean_name = pkg_path.name
                        if clean_name in system_npm_pkgs:
                            continue
                        if _is_system_component(clean_name, pkg_name):
                            continue

                        seen_npm_pkgs.add(pkg_name)
                        size_bytes = 0
                        install_time = 0
                        try:
                            install_time = int(pkg_path.stat().st_mtime)
                            size_bytes = get_size_fast(pkg_path)
                        except OSError:
                            pass

                        size_str = bytes_to_human(size_bytes) if size_bytes > 0 else "N/A"
                        apps.append(
                            _app_record(
                                pkg_name,
                                clean_name,
                                size_bytes,
                                size_str,
                                AppType.NPM,
                                install_time,
                            )
                        )
            except OSError:
                pass
    except OSError:
        pass
    return apps


def _scan_standalone_cli_apps() -> list[AppRecord]:
    home = Path.home()
    discovered_standalone: list[_DiscoveredTool] = []
    skip_cli_names = {
        "pip",
        "python",
        "python3",
        "git",
        "bash",
        "zsh",
        "topo",
        "node",
        "npm",
        "yarn",
        "systemd",
    }
    cli_dir_aliases = {
        "agy": [home / ".gemini"],
    }

    local_bin = home / ".local/bin"
    if local_bin.is_dir():
        try:
            for entry in local_bin.iterdir():
                cmd_name = entry.name.lower()
                if cmd_name in skip_cli_names or cmd_name.startswith("."):
                    continue
                if _is_system_component(cmd_name, cmd_name):
                    continue

                share_dir = home / ".local/share" / cmd_name
                config_dir = home / ".config" / cmd_name
                dot_home_dir = home / f".{cmd_name}"
                alias_dirs = [d for d in cli_dir_aliases.get(cmd_name, []) if d.is_dir()]

                target_dir = (
                    share_dir
                    if share_dir.is_dir()
                    else (
                        config_dir
                        if config_dir.is_dir()
                        else (
                            dot_home_dir
                            if dot_home_dir.is_dir()
                            else (alias_dirs[0] if alias_dirs else None)
                        )
                    )
                )

                if target_dir:
                    discovered_standalone.append(
                        {
                            "id": cmd_name,
                            "name": entry.name,
                            "binary": entry,
                            "install_dir": target_dir,
                        }
                    )
        except OSError:
            pass

    try:
        for item in home.iterdir():
            if item.name.startswith(".") and item.is_dir() and not item.is_symlink():
                tool_name = item.name.lstrip(".")
                if tool_name in skip_cli_names or len(tool_name) < 2:
                    continue
                if _is_system_component(tool_name, tool_name):
                    continue

                bin_sub = item / "bin"
                if bin_sub.is_dir():
                    short_name = tool_name.split("-")[0]
                    matching_bins = [
                        f
                        for f in bin_sub.iterdir()
                        if f.is_file()
                        and (f.name.lower().startswith(tool_name) or f.name.lower() == short_name)
                    ]
                    if matching_bins:
                        main_bin = matching_bins[0]
                        if not any(d["id"] == tool_name for d in discovered_standalone):
                            discovered_standalone.append(
                                {
                                    "id": tool_name,
                                    "name": tool_name,
                                    "binary": main_bin,
                                    "install_dir": item,
                                }
                            )
    except OSError:
        pass

    apps: list[AppRecord] = []
    for tool in discovered_standalone:
        tool_id = tool["id"]
        tool_name = tool["name"]
        bin_path = tool["binary"]
        inst_dir = tool["install_dir"]

        size_bytes = 0
        install_time = 0
        try:
            install_time = int(inst_dir.stat().st_mtime)
            size_bytes = get_size_fast(inst_dir)
            if bin_path.exists():
                size_bytes += (
                    bin_path.lstat().st_size if bin_path.is_symlink() else get_size_fast(bin_path)
                )
        except OSError:
            pass

        size_str = bytes_to_human(size_bytes) if size_bytes > 0 else "N/A"
        record = _app_record(
            tool_id,
            tool_name,
            size_bytes,
            size_str,
            AppType.CLI,
            install_time,
        )
        record["install_dir"] = inst_dir
        apps.append(record)

    return apps


def discover_installed_apps() -> list[AppRecord]:
    """Every app the eight sources can find, in whatever order they answered.

    The pre-scan goes first and alone: three of the seven scanners need its two
    answers -- which packages own an installed ``.desktop`` file, and what display
    name that file carries -- to tell a user-facing package from a library.

    The seven that follow run one thread each. They query seven different tools
    and share nothing, so this is waiting on seven processes at once rather than
    seven times in a row; a desktop with all seven present used to spend the sum
    of their runtimes here. One scanner raising takes only its own source out of
    the list, which is the whole reason the result is collected per future: a
    machine where `snap list` dies should still get its rpm packages.

    Records come back unsorted and without residue sizes. The caller
    (``UninstallManager.run_full_scan``) adds the per-app residue it finds and
    decides the order, because "which paths belong to this app" is a question
    about removal, not about what is installed.
    """
    user_app_packages, package_desktop_names = _pre_scan_package_desktop_names()

    scan_tasks = [
        lambda: _scan_rpm_packages(user_app_packages, package_desktop_names),
        lambda: _scan_apt_packages(user_app_packages, package_desktop_names),
        lambda: _scan_pacman_packages(user_app_packages, package_desktop_names),
        _scan_flatpak_apps,
        lambda: _scan_snap_apps(package_desktop_names),
        _scan_npm_global_packages,
        _scan_standalone_cli_apps,
    ]

    apps: list[AppRecord] = []
    with ThreadPoolExecutor(max_workers=len(scan_tasks)) as executor:
        futures = [executor.submit(task) for task in scan_tasks]
        for future in as_completed(futures):
            with contextlib.suppress(Exception):
                apps.extend(future.result())
    return apps
