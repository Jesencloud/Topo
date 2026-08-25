import contextlib
import os
import re
import shutil
import subprocess
import time
from array import array
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import system
from .core.config import get_use_trash
from .core.constants import (
    RPM_QUERY_BATCH_SIZE,
)
from .core.desktop_entry import get_desktop_exec_names, get_desktop_icon, get_desktop_name
from .core.file_ops import (
    bytes_to_human,
    comm_pattern,
    get_size_fast,
    parse_size_to_bytes,
    record_deletion_audit,
    running_process_comms,
    safe_remove,
)
from .core.history import record_history_session
from .core.package_manager import (
    DNF,
    PACKAGE_QUERY_TOOLS,
    get_rpm_family_manager,
    resolve_admin_tool,
)
from .core.whitelist import LINUX_USER_DATA_DIRS

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

# How long a process gets to act on SIGTERM before it is killed, and how long the
# kernel gets to reap it afterwards. Both are waited through once per selection,
# not once per app.
SIGTERM_GRACE_SECONDS = 1.0
SIGKILL_GRACE_SECONDS = 0.5


def _has_deb_database() -> bool:
    """Whether dpkg-query has anything to answer from."""
    with contextlib.suppress(OSError):
        return _DPKG_STATUS_FILE.stat().st_size > 0
    return False


@dataclass(frozen=True, eq=False)
class _ResidueEntryIndex:
    """One scanned root plus compact lookup tables for residue candidates.

    ``candidates()`` narrows a root's entries down to the ones that could possibly
    satisfy :meth:`UninstallManager._name_matches`; every survivor is still run
    through that method, so the index can only ever cost extra work, never invent
    a match. Each lookup table exists to cover exactly one ``_name_matches``
    branch, and the coverage is what makes the narrowing safe:

    * ``entry == token``               -> ``exact[token]``
    * ``entry.startswith(token + sep)`` -> ``prefixes[token[:3]]``; the branch is
      guarded by ``len(token) >= 3``, and an entry that starts with the token
      shares its first three characters.
    * ``token in entry`` (len >= 5)    -> the gram bucket of ``token[:5]``; a
      contiguous substring's leading 5-gram is necessarily one of the entry's own.

    ``eq=False`` keeps the dataclass from generating ``__eq__``/``__hash__``: this
    is a per-run cache value compared by identity, and a generated ``__eq__``
    would walk 2048 arrays while the paired ``__hash__`` would raise on the dicts.
    """

    entries: tuple[tuple[str, Path], ...]
    exact: dict[str, tuple[int, ...]]
    prefixes: dict[str, tuple[int, ...]]
    gram_buckets: tuple[array, ...]
    is_indexed: bool

    _GRAM_BUCKET_COUNT = 2048
    # Below this many entries the lookup tables cost more than the scan they
    # replace: the gram table alone allocates _GRAM_BUCKET_COUNT arrays (~194 KiB)
    # regardless of how many entries land in them, and roots like
    # ~/.config/systemd/user or ~/snap hold a few dozen at most. Such roots are
    # kept as a plain entry list and candidates() hands back all of them, which is
    # still a valid superset -- just an unnarrowed one.
    _MIN_ENTRIES_TO_INDEX = 512

    @classmethod
    def _gram_bucket(cls, gram: str) -> int:
        # Deliberately str.__hash__, which is randomized per process (PYTHONHASHSEED).
        # Build and query therefore have to happen in the same process: this index
        # must never be pickled, persisted next to the ScanCache/heavy_cache data,
        # or handed to a ProcessPoolExecutor. The current caller builds it in
        # run_full_scan() and consumes it in the ThreadPoolExecutor right
        # after, which shares the interpreter and so shares the hash seed.
        return hash(gram) % cls._GRAM_BUCKET_COUNT

    @classmethod
    def build(cls, entries: list[tuple[str, Path]]) -> "_ResidueEntryIndex":
        normalized = tuple(entries)
        if len(normalized) < cls._MIN_ENTRIES_TO_INDEX:
            return cls(entries=normalized, exact={}, prefixes={}, gram_buckets=(), is_indexed=False)

        exact: dict[str, list[int]] = defaultdict(list)
        prefixes: dict[str, list[int]] = defaultdict(list)
        gram_buckets = tuple(array("I") for _ in range(cls._GRAM_BUCKET_COUNT))
        bucket_of = cls._gram_bucket
        for index, (name, _path) in enumerate(normalized):
            exact[name].append(index)
            if len(name) >= 3:
                prefixes[name[:3]].append(index)
            if len(name) >= 5:
                buckets = {bucket_of(name[offset : offset + 5]) for offset in range(len(name) - 4)}
                for bucket in buckets:
                    gram_buckets[bucket].append(index)
        return cls(
            entries=normalized,
            exact={key: tuple(value) for key, value in exact.items()},
            prefixes={key: tuple(value) for key, value in prefixes.items()},
            gram_buckets=gram_buckets,
            is_indexed=True,
        )

    def candidates(self, targets: set[str]) -> list[tuple[str, Path]]:
        if not self.is_indexed:
            return list(self.entries)
        candidate_ids: set[int] = set()
        for raw_target in targets:
            target = raw_target.strip().lower()
            if not target or target in UninstallManager._GENERIC_TOKENS:
                continue
            candidate_ids.update(self.exact.get(target, ()))
            if len(target) >= 3:
                candidate_ids.update(self.prefixes.get(target[:3], ()))
            if len(target) >= 5:
                candidate_ids.update(self.gram_buckets[self._gram_bucket(target[:5])])
        return [self.entries[index] for index in sorted(candidate_ids)]


class UninstallManager:
    _SCAN_CACHE_TTL_SECONDS = 30
    _scan_cache_apps: list[dict[str, Any]] | None = None
    _scan_cache_time = 0.0
    _scan_cache_key: tuple[Any, ...] | None = None

    # Tokens too short or generic to safely substring-match against folder names.
    # Matching these loosely would flag unrelated directories for deletion
    # (e.g. "desktop" from "org.telegram.desktop", or "data"/"app").
    _GENERIC_TOKENS = frozenset(
        {
            "app",
            "apps",
            "data",
            "core",
            "bin",
            "cache",
            "config",
            "share",
            "gui",
            "lib",
            "tmp",
            "temp",
            "default",
            "common",
            "main",
            "client",
            "desktop",
            "system",
            "settings",
            "local",
            "user",
            "code",
            "go",
            "id",
        }
    )
    _OFFICIAL_ONLY_TOKENS = frozenset(
        {
            "1password",
            "anyconnect",
            "bitwarden",
            "clamav",
            "crowdstrike",
            "defender",
            "eset",
            "fcitx",
            "fcitx5",
            "forticlient",
            "globalprotect",
            "gnupg",
            "gpg",
            "ibus",
            "input-method",
            "inputmethod",
            "keepass",
            "keepassxc",
            "openvpn",
            "rime",
            "security",
            "sentinel",
            "sophos",
            "ssh",
            "tailscale",
            "vpn",
            "wireguard",
            "zerotier",
        }
    )
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

    def __init__(self):
        self.apps: list[dict[str, Any]] = []
        # The residue index run_full_scan built, kept so the preview can reuse it.
        # Only ever valid inside the process that built it: _ResidueEntryIndex
        # buckets grams with the per-process-randomized str.__hash__.
        self._pre_scanned_entries: dict[Path, _ResidueEntryIndex] | None = None

    @classmethod
    def clear_scan_cache(cls) -> None:
        cls._scan_cache_apps = None
        cls._scan_cache_time = 0.0
        cls._scan_cache_key = None

    @staticmethod
    def _desktop_dirs_signature() -> tuple[tuple[str, int, int], ...]:
        signatures = []
        for directory in (
            Path("/usr/share/applications"),
            Path.home() / ".local/share/applications",
        ):
            try:
                stat = directory.stat()
            except OSError:
                continue
            signatures.append((str(directory), int(stat.st_mtime), int(stat.st_size)))
        return tuple(signatures)

    @classmethod
    def _current_scan_cache_key(cls) -> tuple[Any, ...]:
        return (
            system.get_os_id(),
            tuple(bool(shutil.which(tool)) for tool in PACKAGE_QUERY_TOOLS),
            bool(shutil.which("flatpak")),
            bool(shutil.which("snap")),
            cls._desktop_dirs_signature(),
        )

    @classmethod
    def has_fresh_scan_cache(cls) -> bool:
        if cls._scan_cache_apps is None:
            return False
        if cls._scan_cache_key != cls._current_scan_cache_key():
            return False
        return (time.monotonic() - cls._scan_cache_time) <= cls._SCAN_CACHE_TTL_SECONDS

    @staticmethod
    def _name_matches(entry_lower: str, token: str) -> bool:
        """Conservatively decide whether a folder name belongs to an app token.

        Avoids deleting unrelated directories by rejecting short/generic tokens
        and requiring a word boundary for prefix matches. Only distinctive
        tokens (>= 5 chars) are allowed to match as a free substring.
        """
        token = token.strip().lower()
        if not token or token in UninstallManager._GENERIC_TOKENS:
            return False
        if entry_lower == token:
            return True
        if len(token) < 3:
            return False  # too short for any fuzzy matching
        # Word-boundary prefix, e.g. "telegram" -> "telegram-desktop"
        if any(entry_lower.startswith(token + sep) for sep in ("-", "_", ".", " ")):
            return True
        # Distinctive tokens may appear anywhere in the folder name
        return len(token) >= 5 and token in entry_lower

    @staticmethod
    def _app_text(app_id: str, app_name: str) -> str:
        return f"{app_id} {app_name}".lower()

    @classmethod
    def _requires_official_only_uninstall(cls, app_id: str, app_name: str) -> bool:
        text = cls._app_text(app_id, app_name)
        return any(token in text for token in cls._OFFICIAL_ONLY_TOKENS)

    @classmethod
    def _is_system_component(cls, app_id: str, app_name: str) -> bool:
        text = cls._app_text(app_id, app_name)
        app_id_lower = app_id.lower()
        if app_id_lower in cls._SYSTEM_COMPONENT_EXACT_IDS or any(
            token in text for token in cls._SYSTEM_COMPONENT_TOKENS
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

    @staticmethod
    def _strip_package_arch(package_name: str) -> str:
        return package_name.split(":", 1)[0]

    @staticmethod
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

    @staticmethod
    def _app_record(
        app_id: str,
        name: str,
        size_bytes: int,
        size_str: str,
        app_type: str,
        install_time: int = 0,
    ) -> dict[str, Any]:
        return {
            "id": app_id,
            "name": name,
            "size_bytes": size_bytes,
            "size_str": size_str,
            "type": app_type,
            "install_time": install_time,
        }

    def _get_app_keywords(self, desktop_file: Path) -> list[str]:
        """Extracts potential folder name keywords from Exec and Icon fields."""
        keywords = {name.lower() for name in get_desktop_exec_names(desktop_file)}
        icon_name = get_desktop_icon(desktop_file).lower()
        if icon_name:
            keywords.add(icon_name)
        return list(keywords)

    def _candidate_process_names(
        self, app: dict[str, Any], paths: list[Path] | None = None
    ) -> list[str]:
        """Plausible process (comm) names to terminate before removing an app.

        Dynamically discovers process names using:
        1. Package/Flatpak/Snap ID
        2. Binary names parsed from all associated .desktop Exec fields
        3. Active PIDs occupying the app's residue directories via fuser
        4. Name tokens (splitting hyphens/underscores/prefixes)
        """
        names: set[str] = set()
        app_id = str(app.get("id") or "")
        app_name = str(app.get("name") or "")

        if app_id:
            names.add(app_id)
            names.add(app_id.lower())
            if "." in app_id:  # flatpak: org.gnome.Music -> music
                names.add(app_id.rsplit(".", 1)[-1].lower())

        # Generic token splitting: e.g. "google-chrome-stable" -> "chrome", "linuxqq" -> "qq"
        for raw in (app_id, app_name):
            if not raw or " " in raw:
                continue
            lower_raw = raw.lower()
            for prefix in ("linux", "org.", "com.", "net.", "io.", "io.github."):
                if lower_raw.startswith(prefix) and len(lower_raw) > len(prefix) + 2:
                    names.add(lower_raw[len(prefix) :])
            for part in lower_raw.replace("_", "-").split("-"):
                if len(part) >= 3 and part not in (
                    "stable",
                    "beta",
                    "dev",
                    "desktop",
                    "linux",
                    "free",
                    "community",
                ):
                    names.add(part)

        # Dynamic .desktop Exec binary extraction
        desktop_dirs = [
            Path("/usr/share/applications"),
            Path.home() / ".local/share/applications",
            Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications",
        ]
        targets = {t for t in (app_id.lower(), app_name.lower()) if t}
        for ddir in desktop_dirs:
            if not ddir.is_dir():
                continue
            with contextlib.suppress(OSError):
                for entry in ddir.glob("*.desktop"):
                    # Everything collected here ends up as an argument to `pkill -9`,
                    # so the match has to be as strict as the one guarding residue
                    # deletion: a bare substring test would let a two-letter id like
                    # "go" or "qq" pull in half of /usr/share/applications and kill
                    # whatever those entries happen to run. A file named exactly after
                    # the app is still taken, even for a token _name_matches rejects as
                    # generic -- go.desktop is unambiguously the entry for id "go".
                    stem = entry.stem.lower()
                    # Reverse-DNS entries carry the app's own name last:
                    # org.gnome.Music.desktop for org.gnome.Music.
                    haystacks = {stem, stem.rsplit(".", 1)[-1]}
                    if stem in targets or any(
                        self._name_matches(h, t) for h in haystacks for t in targets
                    ):
                        names.update(get_desktop_exec_names(entry))

        # Dynamic fuser / lsof inspection on app residue paths
        if paths:
            for p in paths:
                if p.exists():
                    try:
                        res = system.run_command(["fuser", str(p)], capture=True, timeout=3)
                        stdout_text = str(res.stdout or "")
                        if res.ok and stdout_text.strip():
                            # fuser outputs PIDs like '1234m'; extract pure numeric PIDs
                            for pid_clean in re.findall(r"\b\d+\b", stdout_text):
                                comm_path = Path(f"/proc/{pid_clean}/comm")
                                if comm_path.exists():
                                    with contextlib.suppress(OSError):
                                        comm_name = comm_path.read_text().strip()
                                        if comm_name:
                                            names.add(comm_name)
                    except (OSError, subprocess.SubprocessError):
                        pass

        return [n for n in names if n]

    def _pre_scan_package_desktop_names(self) -> tuple[set[str], dict[str, str]]:
        """Pre-scans native packages that provide desktop files and maps display names."""
        user_app_packages: set[str] = set()
        package_desktop_names: dict[str, str] = {}
        try:
            desktop_dirs = [
                "/usr/share/applications",
                str(Path.home() / ".local/share/applications"),
            ]
            # glob() still yields a dangling symlink, and rpm reports an unreadable
            # path on stderr instead of stdout -- that drops a line from the batch
            # and shifts every later name onto the wrong package. Dropping the
            # unreadable entries here keeps the reply one line per queried path.
            desktop_files: list[Path] = []
            for d in desktop_dirs:
                p = Path(d)
                if p.exists():
                    desktop_files.extend(e for e in p.glob("*.desktop") if e.exists())

            if desktop_files:
                batch_size = RPM_QUERY_BATCH_SIZE
                str_desktop_files = [str(f) for f in desktop_files]
                # One PATH search per tool, not one per batch: a desktop with a few
                # thousand .desktop files splits into many batches, and the answer
                # cannot change while the loop runs. The tools stay additive --
                # a deb box with rpm installed for `alien` must still reach the
                # dpkg branch, or every APT app falls out of the list -- so the
                # box that merely has the *tools* of another distro is excluded by
                # asking whether the database behind them holds anything.
                has_rpm = bool(shutil.which("rpm"))
                has_dpkg_query = bool(shutil.which("dpkg-query")) and _has_deb_database()
                has_pacman = bool(shutil.which("pacman"))
                for i in range(0, len(str_desktop_files), batch_size):
                    batch_str = str_desktop_files[i : i + batch_size]
                    batch_paths = desktop_files[i : i + batch_size]
                    if has_rpm:
                        res = system.run_command(
                            ["rpm", "-qf", "--queryformat", "%{NAME}\n"] + batch_str,
                            capture=True,
                            timeout=60,
                            env=system.C_LOCALE_ENV,
                        )
                        lines = res.stdout.splitlines()
                        # rpm answers positionally and prints its "not owned by any
                        # package" notice on stdout, so a reply of the wrong length
                        # means the answers no longer line up with the questions.
                        # Losing a few display names beats attaching them to the
                        # wrong package.
                        if len(lines) == len(batch_paths):
                            for df_path, line in zip(batch_paths, lines, strict=True):
                                line_str = line.strip()
                                # C_LOCALE_ENV above keeps this marker in English.
                                if line_str and not line_str.startswith("file "):
                                    pkg = line_str
                                    user_app_packages.add(pkg)
                                    if pkg not in package_desktop_names:
                                        dname = get_desktop_name(df_path)
                                        if dname:
                                            package_desktop_names[pkg] = dname
                    if has_dpkg_query:
                        res = system.run_command(
                            ["dpkg-query", "-S", *batch_str],
                            capture=True,
                            env=system.C_LOCALE_ENV,
                        )
                        for line in res.stdout.splitlines():
                            # Every owner of a path shares one comma-separated line
                            # ("procps, libc6:amd64, bash: /usr/share/doc") and an
                            # owner may carry its own :arch, so the path is what
                            # follows the LAST ": " -- splitting on the first colon
                            # cuts libc6:amd64 in half and turns the rest into a
                            # package name that does not exist.
                            owner_str, sep, path_str = line.rpartition(": ")
                            if not sep:
                                continue
                            owners = [
                                pkg
                                for pkg in (
                                    self._strip_package_arch(owner.strip())
                                    for owner in owner_str.split(", ")
                                )
                                if pkg
                            ]
                            user_app_packages.update(owners)
                            if any(pkg not in package_desktop_names for pkg in owners):
                                df_path = Path(path_str.strip())
                                dname = get_desktop_name(df_path) if df_path.exists() else ""
                                if dname:
                                    for pkg in owners:
                                        package_desktop_names.setdefault(pkg, dname)
                    if has_pacman:
                        # " is owned by " sits in pacman's gettext catalog, so the
                        # split below only holds under a C locale.
                        res = system.run_command(
                            ["pacman", "-Qo", *batch_str],
                            capture=True,
                            env=system.C_LOCALE_ENV,
                        )
                        for line in res.stdout.splitlines():
                            if " is owned by " in line:
                                df_str, owned_str = line.split(" is owned by ", 1)
                                owned = owned_str.split()
                                if owned:
                                    pkg = owned[0]
                                    user_app_packages.add(pkg)
                                    if pkg not in package_desktop_names:
                                        df_path = Path(df_str.strip())
                                        if df_path.exists():
                                            dname = get_desktop_name(df_path)
                                            if dname:
                                                package_desktop_names[pkg] = dname
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return user_app_packages, package_desktop_names

    def _scan_rpm_packages(
        self, user_app_packages: set[str], package_desktop_names: dict[str, str]
    ) -> list[dict[str, Any]]:
        if not shutil.which("rpm"):
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
                        if self._is_system_component(app_id, app_id):
                            continue
                        if app_id in user_app_packages or size_bytes > 100 * 1024 * 1024:
                            display_name = package_desktop_names.get(app_id, app_id)
                            apps.append(
                                self._app_record(
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
        self, user_app_packages: set[str], package_desktop_names: dict[str, str]
    ) -> list[dict[str, Any]]:
        if not shutil.which("dpkg-query"):
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
                    app_id = self._strip_package_arch(raw_id)
                    try:
                        size_bytes = int(size_field) * 1024
                    except ValueError:
                        continue
                    # dpkg carries its own answer to "may this be removed", which
                    # covers packages the hardcoded name lists were written for
                    # Fedora and never matched (network-manager, nvidia-dkms-535).
                    if essential == "yes" or priority in ("required", "important"):
                        continue
                    if self._is_system_component(app_id, app_id):
                        continue
                    if app_id in user_app_packages or size_bytes > 100 * 1024 * 1024:
                        install_time = self._dpkg_install_time(raw_id, app_id)
                        display_name = package_desktop_names.get(app_id, app_id)
                        apps.append(
                            self._app_record(
                                app_id,
                                display_name,
                                size_bytes,
                                bytes_to_human(size_bytes),
                                "APT",
                                install_time,
                            )
                        )
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return apps

    def _scan_pacman_packages(
        self, user_app_packages: set[str], package_desktop_names: dict[str, str]
    ) -> list[dict[str, Any]]:
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
                            and not self._is_system_component(app_id, app_id)
                            and (app_id in user_app_packages or size_bytes > 100 * 1024 * 1024)
                        ):
                            display_name = package_desktop_names.get(app_id, app_id)
                            apps.append(
                                self._app_record(
                                    app_id,
                                    display_name,
                                    size_bytes,
                                    bytes_to_human(size_bytes),
                                    "Pacman",
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

    def _scan_flatpak_apps(self) -> list[dict[str, Any]]:
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
                        if self._is_system_component(app_id, app_name):
                            continue

                        size_bytes = parse_size_to_bytes(size_str)
                        apps.append(
                            self._app_record(
                                app_id, app_name, size_bytes, size_str, "Flatpak", install_time
                            )
                        )
        except (OSError, subprocess.SubprocessError):
            pass
        return apps

    def _scan_snap_apps(self, package_desktop_names: dict[str, str]) -> list[dict[str, Any]]:
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
                    if self._is_system_component(app_id, app_id):
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
                        self._app_record(
                            app_id, display_name, size_bytes, size_str, "Snap", install_time
                        )
                    )
        except (OSError, subprocess.SubprocessError):
            pass
        return apps

    def _scan_npm_global_packages(self) -> list[dict[str, Any]]:
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
                            if self._is_system_component(clean_name, pkg_name):
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
                                self._app_record(
                                    pkg_name,
                                    clean_name,
                                    size_bytes,
                                    size_str,
                                    "NPM",
                                    install_time,
                                )
                            )
                except OSError:
                    pass
        except OSError:
            pass
        return apps

    def _scan_standalone_cli_apps(self) -> list[dict[str, Any]]:
        home = Path.home()
        discovered_standalone: list[dict[str, Any]] = []
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
                    if self._is_system_component(cmd_name, cmd_name):
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
                    if self._is_system_component(tool_name, tool_name):
                        continue

                    bin_sub = item / "bin"
                    if bin_sub.is_dir():
                        short_name = tool_name.split("-")[0]
                        matching_bins = [
                            f
                            for f in bin_sub.iterdir()
                            if f.is_file()
                            and (
                                f.name.lower().startswith(tool_name) or f.name.lower() == short_name
                            )
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

        apps = []
        for tool in discovered_standalone:
            tool_id = str(tool["id"])
            tool_name = str(tool["name"])
            bin_path: Path = tool["binary"]
            inst_dir: Path = tool["install_dir"]

            size_bytes = 0
            install_time = 0
            try:
                install_time = int(inst_dir.stat().st_mtime)
                size_bytes = get_size_fast(inst_dir)
                if bin_path.exists():
                    size_bytes += (
                        bin_path.lstat().st_size
                        if bin_path.is_symlink()
                        else get_size_fast(bin_path)
                    )
            except OSError:
                pass

            size_str = bytes_to_human(size_bytes) if size_bytes > 0 else "N/A"
            record = self._app_record(
                tool_id,
                tool_name,
                size_bytes,
                size_str,
                "CLI",
                install_time,
            )
            record["install_dir"] = inst_dir
            apps.append(record)

        return apps

    def _pre_scan_search_roots(self) -> dict[Path, _ResidueEntryIndex]:
        """Scan shared roots once and build reusable residue-candidate indexes."""
        home_path = Path.home()
        search_roots = [
            home_path / ".config",
            home_path / ".local/share",
            home_path / ".local/state",
            home_path / ".cache",
            home_path / ".var/app",
            home_path / "snap",
        ]
        pre_scanned_entries: dict[Path, _ResidueEntryIndex] = {}
        for root in search_roots:
            if root.exists():
                try:
                    entries: list[tuple[str, Path]] = []
                    with os.scandir(root) as it:
                        for item_entry in it:
                            entries.append((item_entry.name.lower(), Path(item_entry.path)))
                    pre_scanned_entries[root] = _ResidueEntryIndex.build(entries)
                except OSError:
                    pass
        for icon_root in (
            home_path / ".local/share/icons",
            home_path / ".local/share/pixmaps",
        ):
            if not icon_root.exists():
                continue
            entries = []
            with contextlib.suppress(OSError):
                for icon_file in icon_root.rglob("*"):
                    if icon_file.is_file():
                        entries.append((icon_file.name.lower(), icon_file))
            pre_scanned_entries[icon_root] = _ResidueEntryIndex.build(entries)

        service_root = home_path / ".config/systemd/user"
        if service_root.exists():
            with contextlib.suppress(OSError):
                pre_scanned_entries[service_root] = _ResidueEntryIndex.build(
                    [(service.name.lower(), service) for service in service_root.glob("*.service")]
                )

        with contextlib.suppress(OSError), os.scandir(home_path) as home_entries:
            pre_scanned_entries[home_path] = _ResidueEntryIndex.build(
                [
                    (entry.name.lower(), Path(entry.path))
                    for entry in home_entries
                    if entry.is_dir(follow_symlinks=False) and entry.name.startswith(".")
                ]
            )
        return pre_scanned_entries

    def _calculate_app_sizes_and_residues(
        self, apps: list[dict[str, Any]], pre_scanned_entries: dict[Path, _ResidueEntryIndex]
    ) -> None:
        """Calculates total app sizes including user data and cache residues in parallel."""

        def _process_single_app(app: dict[str, Any]):
            residue_paths = self.find_residue_paths(
                app["id"], app["name"], pre_scanned_entries=pre_scanned_entries
            )
            inst_dir_val = app.get("install_dir")
            target_inst_dir: Path | None = Path(inst_dir_val).resolve() if inst_dir_val else None
            filtered_residue_paths = [
                p
                for p in residue_paths
                if target_inst_dir is None or Path(p).resolve() != target_inst_dir
            ]

            residue_size = sum(get_size_fast(p) for p in filtered_residue_paths)
            if residue_size > 0:
                app["size_bytes"] += residue_size
                app["size_str"] = bytes_to_human(app["size_bytes"])

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(_process_single_app, apps))

    def run_full_scan(self, *, use_cache: bool = False) -> list[dict[str, Any]]:
        """Scans for user-facing applications across Linux package managers in parallel."""
        cache_key = self._current_scan_cache_key()
        if use_cache and self.has_fresh_scan_cache():
            self.apps = [app.copy() for app in self._scan_cache_apps or []]
            return self.apps

        user_app_packages, package_desktop_names = self._pre_scan_package_desktop_names()

        scan_tasks = [
            lambda: self._scan_rpm_packages(user_app_packages, package_desktop_names),
            lambda: self._scan_apt_packages(user_app_packages, package_desktop_names),
            lambda: self._scan_pacman_packages(user_app_packages, package_desktop_names),
            lambda: self._scan_flatpak_apps(),
            lambda: self._scan_snap_apps(package_desktop_names),
            lambda: self._scan_npm_global_packages(),
            lambda: self._scan_standalone_cli_apps(),
        ]

        apps = []
        with ThreadPoolExecutor(max_workers=len(scan_tasks)) as executor:
            futures = [executor.submit(task) for task in scan_tasks]
            for future in as_completed(futures):
                with contextlib.suppress(Exception):
                    apps.extend(future.result())

        pre_scanned_entries = self._pre_scan_search_roots()
        self._pre_scanned_entries = pre_scanned_entries
        self._calculate_app_sizes_and_residues(apps, pre_scanned_entries)

        self.apps = sorted(
            apps,
            key=lambda x: (x.get("install_time", 0), x.get("size_bytes", 0)),
            reverse=True,
        )

        if use_cache:
            self.__class__._scan_cache_apps = [app.copy() for app in self.apps]
            self.__class__._scan_cache_time = time.monotonic()
            self.__class__._scan_cache_key = cache_key
        return self.apps

    def find_residue_paths(
        self,
        app_id: str,
        app_name: str,
        pre_scanned_entries: dict[Path, _ResidueEntryIndex] | None = None,
    ) -> list[Path]:
        """Finds all data/config/cache paths associated with an app."""
        if self._requires_official_only_uninstall(app_id, app_name):
            return []

        paths = []
        home_path = Path.home()
        seen = set()

        # 1. Standard XDG, Flatpak & Snap paths
        search_roots = [
            home_path / ".config",
            home_path / ".local/share",
            home_path / ".local/state",
            home_path / ".cache",
            home_path / ".var/app",  # Flatpak
            home_path / "snap",  # Snap
        ]

        # 2. Common variants of the name
        targets = {app_id.lower(), app_name.lower()}
        if "." in app_id:
            targets.add(app_id.split(".")[-1].lower())
        if "/" in app_id:
            parts = [p.strip("@").lower() for p in app_id.split("/") if p.strip("@")]
            targets.update(parts)
        for t in list(targets):
            for sep in ("-", "_"):
                if sep in t:
                    prefix = t.rsplit(sep, 1)[0]
                    if len(prefix) >= 3 and prefix not in UninstallManager._GENERIC_TOKENS:
                        targets.add(prefix)

        # Check direct home hidden directories (e.g. ~/.claude, ~/.kimi, ~/.codex, ~/.grok, ~/.cloudbase)
        for t in list(targets):
            if len(t) >= 3:
                dot_dir = home_path / f".{t}"
                if dot_dir.is_dir() and str(dot_dir) not in seen:
                    paths.append(dot_dir)
                    seen.add(str(dot_dir))

        # 3. .desktop file keywords
        desktop_paths = [
            Path(f"/usr/share/applications/{app_id}.desktop"),
            home_path / f".local/share/applications/{app_id}.desktop",
        ]
        for dp in desktop_paths:
            if dp.exists():
                targets.update(self._get_app_keywords(dp))

        # 4. Search using pre-scanned index or fast scandir
        for root in search_roots:
            if not root.exists():
                continue
            if pre_scanned_entries and root in pre_scanned_entries:
                indexed_entries = pre_scanned_entries[root].candidates(targets)
                for entry_lower, p in indexed_entries:
                    if (
                        any(self._name_matches(entry_lower, t) for t in targets)
                        and str(p) not in seen
                    ):
                        paths.append(p)
                        seen.add(str(p))
            else:
                try:
                    with os.scandir(root) as it:
                        for entry in it:
                            entry_lower = entry.name.lower()
                            for t in targets:
                                if self._name_matches(entry_lower, t):
                                    p = Path(entry.path)
                                    if str(p) not in seen:
                                        paths.append(p)
                                        seen.add(str(p))
                except OSError:
                    pass

        # 5. Local Desktop Launchers & Icons
        local_desktop = home_path / ".local/share/applications" / f"{app_id}.desktop"
        if local_desktop.exists() and str(local_desktop) not in seen:
            paths.append(local_desktop)
            seen.add(str(local_desktop))

        icon_roots = [
            home_path / ".local/share/icons",
            home_path / ".local/share/pixmaps",
        ]
        for icon_root in icon_roots:
            if not icon_root.exists():
                continue
            icon_index = pre_scanned_entries.get(icon_root) if pre_scanned_entries else None
            if icon_index is None:
                indexed_icons = []
                with contextlib.suppress(OSError):
                    indexed_icons = [
                        (icon.name.lower(), icon) for icon in icon_root.rglob("*") if icon.is_file()
                    ]
            else:
                indexed_icons = icon_index.candidates(targets)
            for file_lower, icon_file in indexed_icons or []:
                if (
                    any(self._name_matches(file_lower, t) for t in targets)
                    and str(icon_file) not in seen
                ):
                    paths.append(icon_file)
                    seen.add(str(icon_file))

        # 6. Systemd User Services (~/.config/systemd/user/)
        systemd_user_dir = home_path / ".config/systemd/user"
        if systemd_user_dir.exists():
            service_index = (
                pre_scanned_entries.get(systemd_user_dir) if pre_scanned_entries else None
            )
            if service_index is None:
                indexed_services = []
                with contextlib.suppress(OSError):
                    indexed_services = [
                        (service.name.lower(), service)
                        for service in systemd_user_dir.glob("*.service")
                    ]
            else:
                indexed_services = service_index.candidates(targets)
            for file_lower, service_file in indexed_services or []:
                if (
                    any(self._name_matches(file_lower, t) for t in targets)
                    and str(service_file) not in seen
                ):
                    paths.append(service_file)
                    seen.add(str(service_file))

        # 7. Wine prefix check (optional, if wechat/etc)
        if "wechat" in app_name.lower():
            wine_p = home_path / ".xwechat"
            if wine_p.exists() and str(wine_p) not in seen:
                paths.append(wine_p)
                seen.add(str(wine_p))

        # 8. Deep Subdirectory Search (home-root residue).
        # Only HIDDEN dot-directories at the home root (e.g. ~/.someapp) are
        # considered here. Visible top-level home folders are user workspaces and
        # data — ~/Projects, ~/IdeaProjects, ~/studio-projects, ~/notes-backup,
        # ~/VirtualBox VMs — and must NEVER be matched as residue: a fuzzy name
        # hit there would permanently delete the user's own files. XDG user-data
        # dirs are excluded too (defence in depth; they are visible anyway).
        if len(app_name) > 3:
            protected_dir_names = {d.lower() for d in LINUX_USER_DATA_DIRS}
            home_index = pre_scanned_entries.get(home_path) if pre_scanned_entries else None
            if home_index is None:
                try:
                    with os.scandir(home_path) as it:
                        indexed_home = [
                            (entry.name.lower(), Path(entry.path))
                            for entry in it
                            if entry.is_dir(follow_symlinks=False) and entry.name.startswith(".")
                        ]
                except OSError:
                    indexed_home = []
            else:
                indexed_home = home_index.candidates({app_name.lower()})
            for entry_lower, entry_path in indexed_home:
                if entry_lower in protected_dir_names:
                    continue
                if (
                    self._name_matches(entry_lower, app_name.lower())
                    and str(entry_path) not in seen
                    and home_path in entry_path.parents
                ):
                    paths.append(entry_path)
                    seen.add(str(entry_path))

        return paths

    def _collateral_packages(self, app: dict[str, Any]) -> list[str]:
        """Which other installed packages this app's removal will take with it.

        `apt-get purge`, `dnf remove`, `pacman -Rns` and `zypper remove` all pull
        out whatever depends on the package being removed, so ticking one small
        entry can drag out half a desktop -- and the preview only ever listed the
        entry itself and the residue paths beside it.

        Every query here is read-only and runs as the invoking user, because the
        preview is drawn before the password is asked for, and asking for one
        earlier just to draw it would undo the point of only asking when a removal
        needs root. That rules out the exact dry-runs on the rpm side (`dnf remove
        --assumeno`, `rpm -e --test` and `zypper remove --dry-run` all want the
        database lock), so those two are asked what requires the package instead:
        apt and pacman answer with the whole transitive set they would really
        remove, the rpm family with its first level. The list is therefore a floor
        rather than a promise, and a failed or unparsable reply yields an empty
        one -- the preview then says nothing, exactly as it did before.
        """
        app_id = str(app.get("id") or "")
        app_type = str(app.get("type") or "")
        if not app_id:
            return []
        if app_type == "APT":
            # -s simulates without root; the whole transaction is narrated, and
            # the removal lines are the interesting ones.
            argv = ["apt-get", "purge", "-s", app_id]
            env = system.APT_NONINTERACTIVE_ENV
        elif app_type == "Pacman":
            # --print-format implies --print, and --print is what makes pacman
            # skip the database lock it would otherwise need root for. %n asks
            # for bare names, so nothing here goes through a message catalog.
            argv = ["pacman", "-Rns", "--print-format", "%n", app_id]
            env = system.C_LOCALE_ENV
        elif app_type == "DNF":
            dnf_cmd = resolve_admin_tool(DNF)
            # -C keeps it off the network: the installed set is all we ask about.
            argv = [
                dnf_cmd,
                "repoquery",
                "-C",
                "--installed",
                "--whatrequires",
                app_id,
                "--qf",
                "%{name}\n",
            ]
            env = system.C_LOCALE_ENV
        elif app_type == "Zypper":
            # zypper has no unprivileged dry-run, and rpm is on every zypper box.
            argv = ["rpm", "-q", "--whatrequires", app_id, "--qf", "%{NAME}\n"]
            env = system.C_LOCALE_ENV
        else:
            # A Flatpak, Snap, NPM or CLI removal takes nothing else with it.
            return []

        # run_command turns a missing binary or a timeout into a CommandResult
        # rather than raising, and the parser below keeps only bare package names,
        # so a failed or half-finished reply comes out as an empty list. rpm exits
        # 1 with "no package requires X" on stdout when there are none, which is
        # why the return code is not consulted.
        res = system.run_command(argv, capture=True, timeout=30, env=env)
        return self._parse_collateral(res.stdout, app_id, app_type)

    @staticmethod
    def _parse_collateral(stdout: str, app_id: str, app_type: str) -> list[str]:
        """Package names out of a simulated removal or a reverse-dependency reply."""
        names: list[str] = []
        for line in stdout.splitlines():
            entry = line.strip()
            if app_type == "APT":
                # "Remv firefox [1:2snap1-0ubuntu2]", among Inst/Conf lines and
                # apt's own prose.
                fields = entry.split()
                if len(fields) < 2 or fields[0] not in ("Remv", "Purg"):
                    continue
                # A foreign-arch package is narrated qualified (libfoo:i386) while
                # the scan stripped the qualifier off its id, so without this the
                # app fails to recognise itself in its own transaction.
                entry = UninstallManager._strip_package_arch(fields[1])
            # rpm reports "no package requires X" on stdout, and a package name
            # never holds whitespace, so this drops prose without matching on it.
            if not entry or len(entry.split()) != 1 or entry == app_id:
                continue
            if entry not in names:
                names.append(entry)
        return names

    def build_removal_targets(
        self, apps: list[dict[str, Any]]
    ) -> list[tuple[dict[str, Any], list[Path], bool]]:
        """Resolve residue paths and running state for each app about to be removed.

        Both answers cost real work -- residue discovery walks the filesystem and
        the running check has to look at every process on the machine -- so they
        are computed once here rather than from inside a render loop. The result
        is exactly what UninstallPreviewSelector needs to draw the confirmation.

        The index run_full_scan already built is reused: without it every selected
        app re-scandirs the six search roots and fully recurses ~/.local/share/icons
        and pixmaps, directories that routinely hold tens of thousands of files.
        It is also the same snapshot the sizes in the app list were computed from,
        so the preview cannot disagree with the row the user just picked.

        Each app also learns which other packages its removal would drag out, in
        app["collateral_packages"]; the tuple keeps its three fields so callers
        that only want the paths and the running flag are untouched.
        """
        # One /proc pass for the whole selection, instead of a `pgrep -x` fork per
        # candidate name per app -- ten apps with thirty candidate names each used
        # to mean three hundred forks, and the execution pass then repeated them.
        running = running_process_comms()
        # Each collateral query is a fork that spends its time waiting on a package
        # database, so the selection's queries overlap. There is no spinner on
        # screen between the app list and the preview, which is the whole reason
        # this is worth doing rather than accepting a stall per selected app. The
        # types that take nothing else with them cost a return, not a fork, so
        # they go through the same pool rather than being listed a second time.
        if apps:
            with ThreadPoolExecutor(max_workers=8) as pool:
                for app, collateral in zip(
                    apps, pool.map(self._collateral_packages, apps), strict=True
                ):
                    app["collateral_packages"] = collateral
        targets = []
        for app in apps:
            app_paths = self.find_residue_paths(
                app["id"], app["name"], pre_scanned_entries=self._pre_scanned_entries
            )
            is_running = any(
                comm_pattern(proc) in running
                for proc in self._candidate_process_names(app, app_paths)
            )
            targets.append((app, app_paths, is_running))
        return targets

    def _terminate_process_patterns(self, patterns: list[str]) -> None:
        """SIGTERM the given comm patterns, wait once, then SIGKILL the survivors.

        The two waits are per call, not per pattern, so the caller decides how
        often they are paid: terminate_apps closes a whole selection in one 1.5 s
        window, where a per-app kill spent that on every app in turn.
        """
        if not patterns:
            return
        for pattern in patterns:
            system.run_command(["pkill", "-15", "-x", pattern], capture=True, timeout=5)

        time.sleep(SIGTERM_GRACE_SECONDS)

        # One more /proc pass tells us who ignored SIGTERM; the alternative is a
        # `pgrep -x` per pattern.
        survivors = running_process_comms()
        killed = False
        for pattern in patterns:
            if pattern in survivors:
                system.run_command(["pkill", "-9", "-x", pattern], capture=True, timeout=5)
                killed = True
        if killed:
            time.sleep(SIGKILL_GRACE_SECONDS)

    def terminate_apps(self, targets: list[tuple[dict[str, Any], list[Path], bool]]) -> None:
        """Close every selected app's processes before the removals start.

        execute_uninstall still does this for its own app, so this method is an
        optimisation rather than a prerequisite: doing it for the whole selection
        at once means the SIGTERM grace period is waited through once instead of
        once per app, and the per-app step then finds nothing left to kill and
        waits not at all. Ten apps used to spend fifteen seconds here.
        """
        running = running_process_comms()
        patterns: list[str] = []
        for app, paths, _ in targets:
            if app.get("type") == "Flatpak":
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    system.run_command(
                        ["flatpak", "kill", str(app["id"])], capture=True, timeout=20
                    )
            for proc in self._candidate_process_names(app, paths):
                pattern = comm_pattern(proc)
                if pattern in running and pattern not in patterns:
                    patterns.append(pattern)
        self._terminate_process_patterns(patterns)

    def execute_uninstall(self, app: dict[str, Any], paths: list[Path]):
        """Terminates app and removes all files."""
        app_name = str(app.get("name") or app.get("id") or "unknown")
        session_command = f"uninstall {app_name}"
        record_history_session(session_command, "started")
        package_status = "failed"
        package_event_recorded = False
        package_mode = str(app.get("type", "package")).lower()
        package_size = int(app.get("size_bytes") or 0)

        try:
            # 1. Graceful Kill (SIGTERM -> Wait -> SIGKILL). Use real executable
            # names (id + .desktop Exec), never the localized display name.
            all_process_names = self._candidate_process_names(app, paths)
            if app["type"] == "Flatpak":
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    system.run_command(["flatpak", "kill", app["id"]], capture=True, timeout=20)

            # Patterns go through comm_pattern so a long executable name still
            # matches -- and still gets signalled. Which of them are actually
            # running is one /proc read for all of them; when terminate_apps has
            # already closed the selection this list comes back empty and the
            # grace periods are skipped entirely.
            running = running_process_comms()
            processes_to_kill: list[str] = []
            for proc in all_process_names:
                pattern = comm_pattern(proc)
                if pattern in running and pattern not in processes_to_kill:
                    processes_to_kill.append(pattern)

            self._terminate_process_patterns(processes_to_kill)

            # 2. Binary uninstall
            if app["type"] == "Flatpak":
                res = system.run_command(["flatpak", "uninstall", "-y", app["id"]], capture=True)
            elif app["type"] == "Snap":
                res = system.run_command(
                    ["snap", "remove", "--purge", app["id"]], use_sudo=True, capture=True
                )
            elif app["type"] == "NPM":
                res = system.run_command(
                    ["npm", "uninstall", "-g", app["id"]], capture=True, timeout=60
                )
                # Clean empty scoped directory in global node_modules (e.g. ~/.npm-global/lib/node_modules/@cloudbase)
                if "/" in app["id"]:
                    scope = app["id"].split("/")[0]
                    res_root = system.run_command(["npm", "root", "-g"], capture=True, timeout=5)
                    if res_root.ok and res_root.stdout.strip():
                        scope_dir = Path(res_root.stdout.strip()) / scope
                        if scope_dir.is_dir():
                            with contextlib.suppress(OSError):
                                if not any(scope_dir.iterdir()):
                                    scope_dir.rmdir()
            elif app["type"] == "CLI":
                # Remove standalone binary & install directory
                home_p = Path.home()
                cli_targets = [
                    home_p / ".local/bin" / app["id"],
                    home_p / ".local/share" / app["id"],
                    home_p / f".{app['id']}",
                ]
                for ct in cli_targets:
                    if ct.exists():
                        safe_remove(ct, use_trash=get_use_trash(), allow_app_data_removal=True)
                res = system.CommandResult(
                    args=["cli_uninstall"], returncode=0, stdout="CLI uninstalled"
                )
            elif app["type"] == "APT":
                # apt-get, not apt: apt prints "WARNING: apt does not have a stable
                # CLI interface" when its output is captured, and the rest of the
                # repository already standardises on apt-get.
                res = system.run_command(
                    ["apt-get", "purge", "-y", app["id"]],
                    use_sudo=True,
                    capture=True,
                    env=system.APT_NONINTERACTIVE_ENV,
                )
            elif app["type"] == "Pacman":
                res = system.run_command(
                    ["pacman", "-Rns", "--noconfirm", app["id"]],
                    use_sudo=True,
                    capture=True,
                )
            elif app["type"] == "Zypper":
                res = system.run_command(
                    # --clean-deps for the same reason the screen follows an apt
                    # removal with `apt-get autoremove --purge`: dnf drops the
                    # dependencies nothing needs any more by default and pacman is
                    # asked to with -Rns, while zypper keeps them unless told,
                    # which would leave openSUSE the one family where uninstalling
                    # quietly leaves orphans on disk.
                    ["zypper", "--non-interactive", "remove", "--clean-deps", app["id"]],
                    use_sudo=True,
                    capture=True,
                    env=system.C_LOCALE_ENV,
                )
            elif app["type"] == "DNF":
                dnf_cmd = resolve_admin_tool(DNF)
                res = system.run_command(
                    [dnf_cmd, "remove", "-y", app["id"]], use_sudo=True, capture=True
                )
            else:
                # Named explicitly rather than fallen through to: the old else ran
                # `dnf remove` for anything it did not recognise, which on a
                # zypper or an unlabelled entry meant a removal that could not
                # work. Failing says so; guessing a package manager does not.
                res = system.CommandResult(
                    args=["unsupported"],
                    returncode=1,
                    error=f"unsupported package type: {app['type']}",
                )
            package_status = "removed" if res.ok else "failed"
            record_deletion_audit(app["id"], package_mode, package_status, package_size)
            package_event_recorded = True

            # 3. Path removal: explicit uninstall may remove app-owned data,
            # but hard-protected credentials/system paths remain blocked.
            removed_details = []
            removed_systemd_service = False
            # Residue removal is recoverable (trash) rather than a permanent
            # wipe: residue discovery is heuristic, so a mis-matched user
            # directory must be undoable. config.json's use_trash=false is the
            # one way to ask for an unrecoverable wipe instead.
            # allow_app_data_removal still lets app-owned data go, while
            # hard-protected paths (whitelist, credentials, system, XDG
            # user-data dirs) stay blocked.
            use_trash = get_use_trash()
            for p in paths:
                success, _ = safe_remove(p, use_trash=use_trash, allow_app_data_removal=True)
                if success and str(p).endswith(".service") and ".config/systemd/user" in str(p):
                    removed_systemd_service = True
                try:
                    removed_details.append((success, str(p.relative_to(Path.home()))))
                except ValueError:
                    removed_details.append((success, str(p)))

            if removed_systemd_service and shutil.which("systemctl"):
                system.run_command(
                    ["systemctl", "--user", "daemon-reload"], capture=True, timeout=10
                )

            return {
                "package_removed": package_status == "removed",
                "removed_paths": removed_details,
            }
        finally:
            if package_status == "failed" and not package_event_recorded:
                record_deletion_audit(app.get("id", app_name), package_mode, "failed", package_size)
            record_history_session(session_command, "ended")
