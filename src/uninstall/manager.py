import contextlib
import os
import re
import shutil
import subprocess
import time
from array import array
from collections import defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Any

from ..core import system
from ..core.config import get_use_trash
from ..core.constants import AppType
from ..core.desktop_entry import get_desktop_exec_names, get_desktop_icon
from ..core.file_ops import (
    bytes_to_human,
    comm_pattern,
    get_size_fast,
    record_deletion_audit,
    running_process_comms,
    safe_remove,
)
from ..core.history import record_history_session
from ..core.package_manager import (
    DNF,
    PACKAGE_QUERY_TOOLS,
    resolve_admin_tool,
)
from ..core.whitelist import LINUX_USER_DATA_DIRS
from .discovery import (
    AppRecord,
    app_text,
    discover_installed_apps,
    strip_package_arch,
)

# How long a process gets to act on SIGTERM before it is killed, and how long the
# kernel gets to reap it afterwards. Both are waited through once per selection,
# not once per app.
SIGTERM_GRACE_SECONDS = 1.0
SIGKILL_GRACE_SECONDS = 0.5


def _is_sandbox_app_data(path: Path) -> bool:
    """Whether this path is the single directory a sandboxed app keeps everything in.

    `~/.var/app/<app-id>` (Flatpak) and `~/snap/<name>` (Snap) are not a cache
    and not one config file: they are the whole of the app's user data in one
    directory -- a browser's bookmarks and saved passwords included, since a
    sandboxed browser has nowhere else to put them. So they go to the trash even
    when config.json asked for a permanent wipe: `use_trash=false` is a request
    to actually free the space a cache occupies, not a waiver on data that
    cannot be regenerated. Only the directory named after the app qualifies;
    anything deeper is already inside it and travels with it.

    Deciding this by directory root rather than by application means there is no
    per-app list to keep: whichever app is being removed, this is where a
    sandbox puts its data.
    """
    home = Path.home()
    return path.parent in (home / ".var/app", home / "snap")


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


class _ResiduePathSet:
    """The residue paths found for one app, in discovery order, without duplicates.

    Every step of :meth:`UninstallManager.find_residue_paths` adds through here,
    so there is one answer to "have we already got this?" instead of eight. The
    eight steps used to each write the invariant out by hand --
    ``if ... and str(p) not in seen: paths.append(p); seen.add(str(p))`` -- which
    is two statements that have to stay in step across 176 lines, and a step that
    forgot the second one would have listed a path twice in the removal preview.

    Keyed on ``str(path)`` because that is what the hand-written pairs compared,
    and every step builds its paths from ``Path.home()``, so two steps that reach
    the same file produce the same string. (``Path`` hashes by its parts and would
    dedup these just as well; the string keeps the old behaviour exactly.)
    """

    def __init__(self) -> None:
        self._paths: list[Path] = []
        self._seen: set[str] = set()

    def add(self, path: Path) -> None:
        key = str(path)
        if key not in self._seen:
            self._seen.add(key)
            self._paths.append(path)

    def as_list(self) -> list[Path]:
        return list(self._paths)


class UninstallManager:
    _SCAN_CACHE_TTL_SECONDS = 30
    _scan_cache_apps: list[AppRecord] | None = None
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

    def __init__(self):
        self.apps: list[AppRecord] = []
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

    @classmethod
    def _requires_official_only_uninstall(cls, app_id: str, app_name: str) -> bool:
        text = app_text(app_id, app_name)
        return any(token in text for token in cls._OFFICIAL_ONLY_TOKENS)

    def _get_app_keywords(self, desktop_file: Path) -> list[str]:
        """Extracts potential folder name keywords from Exec and Icon fields."""
        keywords = {name.lower() for name in get_desktop_exec_names(desktop_file)}
        icon_name = get_desktop_icon(desktop_file).lower()
        if icon_name:
            keywords.add(icon_name)
        return list(keywords)

    def _candidate_process_names(
        self, app: AppRecord, paths: list[Path] | None = None
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
        for source_name in (app_id, app_name):
            if not source_name or " " in source_name:
                continue
            lowered = source_name.lower()
            for prefix in ("linux", "org.", "com.", "net.", "io.", "io.github."):
                if lowered.startswith(prefix) and len(lowered) > len(prefix) + 2:
                    names.add(lowered[len(prefix) :])
            for part in lowered.replace("_", "-").split("-"):
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
        targets = {name for name in (app_id.lower(), app_name.lower()) if name}
        for desktop_dir in desktop_dirs:
            if not desktop_dir.is_dir():
                continue
            with contextlib.suppress(OSError):
                for entry in desktop_dir.glob("*.desktop"):
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
                    entry_names = {stem, stem.rsplit(".", 1)[-1]}
                    if stem in targets or any(
                        self._name_matches(entry_name, target)
                        for entry_name in entry_names
                        for target in targets
                    ):
                        names.update(get_desktop_exec_names(entry))

        # Dynamic fuser / lsof inspection on app residue paths
        if paths:
            for residue_path in paths:
                if residue_path.exists():
                    try:
                        fuser = system.run_command(
                            ["fuser", str(residue_path)], capture=True, timeout=3
                        )
                        stdout_text = str(fuser.stdout or "")
                        if fuser.ok and stdout_text.strip():
                            # fuser outputs PIDs like '1234m'; extract pure numeric PIDs
                            for pid_clean in re.findall(r"\b\d+\b", stdout_text):
                                comm_path = Path(f"/proc/{pid_clean}/comm")
                                if comm_path.exists():
                                    with contextlib.suppress(OSError):
                                        # prctl() lets a process name itself with
                                        # any 15 bytes; suppress(OSError) does not
                                        # catch the UnicodeDecodeError a strict
                                        # decode would raise on them.
                                        comm_name = comm_path.read_text(errors="replace").strip()
                                        if comm_name:
                                            names.add(comm_name)
                    except (OSError, subprocess.SubprocessError):
                        pass

        return [name for name in names if name]

    def _pre_scan_search_roots(self) -> dict[Path, _ResidueEntryIndex]:
        """Scan shared roots once and build reusable residue-candidate indexes.

        The roots are the ones find_residue_paths() searches, taken from the same
        two helpers it takes them from, so neither side can grow a root the other
        does not know about.
        """
        home_path = Path.home()
        pre_scanned_entries: dict[Path, _ResidueEntryIndex] = {}
        for root in self._residue_search_roots(home_path):
            if root.exists():
                try:
                    entries: list[tuple[str, Path]] = []
                    with os.scandir(root) as it:
                        for item_entry in it:
                            entries.append((item_entry.name.lower(), Path(item_entry.path)))
                    pre_scanned_entries[root] = _ResidueEntryIndex.build(entries)
                except OSError:
                    pass
        for icon_root in self._residue_icon_roots(home_path):
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
        self, apps: list[AppRecord], pre_scanned_entries: dict[Path, _ResidueEntryIndex]
    ) -> None:
        """Calculates total app sizes including user data and cache residues in parallel."""

        def _process_single_app(app: AppRecord):
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

    def run_full_scan(self, *, use_cache: bool = False) -> list[AppRecord]:
        """Every installed app, newest and largest first, with its residue counted in.

        The finding is ``discovery``'s, one thread per package manager.
        What this adds is the part that only removal cares about: each app's
        residue paths, found through one shared index of the roots they live in,
        and their size folded into the app's own so the list is ordered by what
        removing it would actually free.
        """
        cache_key = self._current_scan_cache_key()
        if use_cache and self.has_fresh_scan_cache():
            self.apps = [app.copy() for app in self._scan_cache_apps or []]
            return self.apps

        apps = discover_installed_apps()

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
        """Every per-user path that belongs to one app: config, cache, data, launcher.

        This list is what the removal screen previews and what
        ``_remove_residue_paths`` then deletes, so each step below is deliberately
        narrow: see :meth:`_name_matches` for what "named after the app" is allowed
        to mean, and :meth:`_add_home_root_hidden_dirs` for why a visible folder in
        the home directory is never a candidate.

        Two things about the order. Every step adds through one
        :class:`_ResiduePathSet`, so the order they run in is the order the preview
        lists paths in. And ``_add_home_dot_dirs`` runs against the name variants
        *before* the launcher keywords widen them: moving the keyword step above it
        would let a ``~/.<Exec name>`` directory be found too, which is a change in
        what gets deleted rather than a reordering, and is left alone here.
        """
        if self._requires_official_only_uninstall(app_id, app_name):
            return []

        home_path = Path.home()
        found = _ResiduePathSet()
        targets = self._residue_name_targets(app_id, app_name)
        self._add_home_dot_dirs(found, home_path, targets)
        targets |= self._desktop_launcher_keywords(app_id, home_path)
        self._add_app_data_dirs(found, home_path, targets, pre_scanned_entries)
        self._add_launcher_and_icons(found, app_id, home_path, targets, pre_scanned_entries)
        self._add_systemd_user_services(found, home_path, targets, pre_scanned_entries)
        self._add_hardcoded_wine_prefix(found, app_name, home_path)
        self._add_home_root_hidden_dirs(found, app_name, home_path, pre_scanned_entries)
        return found.as_list()

    @staticmethod
    def _residue_search_roots(home_path: Path) -> list[Path]:
        """The per-user roots an app may keep a directory of its own in.

        The XDG trio plus ~/.cache, and the two sandbox roots that hand an app a
        directory named after its id (~/.var/app for Flatpak, ~/snap for Snap).
        Read by both the pre-scan that indexes these roots and the search that
        consumes the index, which is why it is one list: a root indexed but not
        searched costs a scan for nothing, and a root searched but not indexed
        silently falls back to scanning per app.
        """
        return [
            home_path / ".config",
            home_path / ".local/share",
            home_path / ".local/state",
            home_path / ".cache",
            home_path / ".var/app",  # Flatpak
            home_path / "snap",  # Snap
        ]

    @staticmethod
    def _residue_icon_roots(home_path: Path) -> list[Path]:
        """Where a user-installed app drops its icons. Shared with the pre-scan."""
        return [
            home_path / ".local/share/icons",
            home_path / ".local/share/pixmaps",
        ]

    @staticmethod
    def _residue_name_targets(app_id: str, app_name: str) -> set[str]:
        """The names a directory belonging to this app could plausibly be spelled with.

        An id is often not what the app is called on disk: the last segment of a
        Flatpak id (``org.gnome.Music`` -> ``music``) and the parts of an npm scope
        (``@scope/pkg``) both name the same app, and a ``-``/``_`` suffix is
        frequently absent from the directory (``code-insiders`` -> ``code``). The
        suffix-stripped prefix is only kept when it is at least three characters
        and not one of the generic tokens, because a target like ``data`` would go
        on to match unrelated directories -- :meth:`_name_matches` rejects those
        tokens as well, so this is the first of two gates rather than the only one.
        """
        targets = {app_id.lower(), app_name.lower()}
        if "." in app_id:
            targets.add(app_id.split(".")[-1].lower())
        if "/" in app_id:
            targets.update(part.strip("@").lower() for part in app_id.split("/") if part.strip("@"))
        for target in list(targets):
            for separator in ("-", "_"):
                if separator in target:
                    prefix = target.rsplit(separator, 1)[0]
                    if len(prefix) >= 3 and prefix not in UninstallManager._GENERIC_TOKENS:
                        targets.add(prefix)
        return targets

    def _desktop_launcher_keywords(self, app_id: str, home_path: Path) -> set[str]:
        """Extra name targets taken from the app's own .desktop file.

        The Exec and Icon fields are the one place that records what an app calls
        itself on disk, which is regularly neither its package id nor its display
        name. Both launcher locations are read: a package installs into
        /usr/share/applications, a user or a Flatpak override into ~/.local.
        """
        keywords: set[str] = set()
        for desktop_file in (
            Path(f"/usr/share/applications/{app_id}.desktop"),
            home_path / f".local/share/applications/{app_id}.desktop",
        ):
            if desktop_file.exists():
                keywords.update(self._get_app_keywords(desktop_file))
        return keywords

    @staticmethod
    def _add_home_dot_dirs(found: _ResiduePathSet, home_path: Path, targets: set[str]) -> None:
        """``~/.<name>`` directories, which no root below covers.

        A tool that predates XDG keeps its state straight in the home directory:
        ~/.claude, ~/.kimi, ~/.codex, ~/.grok, ~/.cloudbase. Matched exactly, not
        fuzzily -- :meth:`_add_home_root_hidden_dirs` is the fuzzy pass over the
        same directory, and it carries the guards that a fuzzy match at the home
        root needs.
        """
        for target in targets:
            if len(target) >= 3:
                dot_dir = home_path / f".{target}"
                if dot_dir.is_dir():
                    found.add(dot_dir)

    @staticmethod
    def _residue_candidates(
        index: _ResidueEntryIndex | None,
        targets: set[str],
        list_entries: Callable[[], list[tuple[str, Path]]],
    ) -> list[tuple[str, Path]]:
        """The ``(lowercased name, path)`` pairs worth matching in one root.

        ``run_full_scan`` scans the shared roots once and hands every app the
        resulting index; a single ``topo uninstall <app>`` has no index and lists
        the root here instead. Both arms return a list, and every pair either arm
        returns is still put through :meth:`_name_matches`, so an index can only
        narrow the work, never decide the answer.

        One shape for all three indexed roots. They used to be written three
        different ways -- two ended in ``for ... in indexed_x or []`` and the third
        did not -- and since neither fallback could produce None, the two ``or []``
        were dead code that made the third site look like the one with the bug.
        """
        if index is not None:
            return index.candidates(targets)
        return list_entries()

    @staticmethod
    def _scandir_entries(root: Path, *, hidden_dirs_only: bool = False) -> list[tuple[str, Path]]:
        """One directory's entries as ``(lowercased name, path)``, best effort.

        Whatever was read before an OSError is kept rather than discarded: the
        result is a list of deletion candidates, every one of which is matched and
        filtered afterwards, so a root that goes unreadable half way through
        (a directory removed by something else while topo scans it) costs the
        entries behind the failure and nothing more.
        """
        entries: list[tuple[str, Path]] = []
        with contextlib.suppress(OSError), os.scandir(root) as scan:
            for entry in scan:
                if hidden_dirs_only and not (
                    entry.name.startswith(".") and entry.is_dir(follow_symlinks=False)
                ):
                    continue
                entries.append((entry.name.lower(), Path(entry.path)))
        return entries

    @staticmethod
    def _named_entries(paths: Iterable[Path]) -> list[tuple[str, Path]]:
        """A listing's results as ``(lowercased name, path)``, best effort.

        Same partial-result contract as :meth:`_scandir_entries`.
        """
        entries: list[tuple[str, Path]] = []
        with contextlib.suppress(OSError):
            for path in paths:
                entries.append((path.name.lower(), path))
        return entries

    @classmethod
    def _icon_file_entries(cls, icon_root: Path) -> list[tuple[str, Path]]:
        """Every file under one icon root, at any depth: icons sit in theme/size subdirs.

        A method rather than an expression at the call site so that the walk starts
        only when there is no index to answer from -- the leading iterable of a
        generator expression is evaluated where it is written, which would have
        called rglob() even on the indexed path.
        """
        return cls._named_entries(icon for icon in icon_root.rglob("*") if icon.is_file())

    @classmethod
    def _service_file_entries(cls, service_root: Path) -> list[tuple[str, Path]]:
        """The unit files in ~/.config/systemd/user; flat, so not recursive."""
        return cls._named_entries(service_root.glob("*.service"))

    def _add_app_data_dirs(
        self,
        found: _ResiduePathSet,
        home_path: Path,
        targets: set[str],
        pre_scanned_entries: dict[Path, _ResidueEntryIndex] | None,
    ) -> None:
        """Directories named after the app in the XDG and sandbox roots."""
        for root in self._residue_search_roots(home_path):
            if not root.exists():
                continue
            index = pre_scanned_entries.get(root) if pre_scanned_entries else None
            for entry_lower, entry_path in self._residue_candidates(
                index, targets, partial(self._scandir_entries, root)
            ):
                if any(self._name_matches(entry_lower, target) for target in targets):
                    found.add(entry_path)

    def _add_launcher_and_icons(
        self,
        found: _ResiduePathSet,
        app_id: str,
        home_path: Path,
        targets: set[str],
        pre_scanned_entries: dict[Path, _ResidueEntryIndex] | None,
    ) -> None:
        """The app's user-level .desktop launcher, and icon files named after it.

        Left behind, these are what keeps a removed app in the application grid
        with its icon intact.
        """
        local_desktop = home_path / ".local/share/applications" / f"{app_id}.desktop"
        if local_desktop.exists():
            found.add(local_desktop)

        for icon_root in self._residue_icon_roots(home_path):
            if not icon_root.exists():
                continue
            index = pre_scanned_entries.get(icon_root) if pre_scanned_entries else None
            for file_lower, icon_file in self._residue_candidates(
                index, targets, partial(self._icon_file_entries, icon_root)
            ):
                if any(self._name_matches(file_lower, target) for target in targets):
                    found.add(icon_file)

    def _add_systemd_user_services(
        self,
        found: _ResiduePathSet,
        home_path: Path,
        targets: set[str],
        pre_scanned_entries: dict[Path, _ResidueEntryIndex] | None,
    ) -> None:
        """User units named after the app, which would otherwise keep starting it."""
        service_root = home_path / ".config/systemd/user"
        if not service_root.exists():
            return
        index = pre_scanned_entries.get(service_root) if pre_scanned_entries else None
        for file_lower, service_file in self._residue_candidates(
            index, targets, partial(self._service_file_entries, service_root)
        ):
            if any(self._name_matches(file_lower, target) for target in targets):
                found.add(service_file)

    @staticmethod
    def _add_hardcoded_wine_prefix(found: _ResiduePathSet, app_name: str, home_path: Path) -> None:
        """WeChat's Wine prefix -- the one path here that is written out, not derived.

        Every other step matches names the app itself supplies (its id, its display
        name, its launcher's Exec and Icon fields) against what is on disk.
        ``~/.xwechat`` is none of those names: the prefix is created by the vendor's
        own launcher under a name of its choosing, so the only reason topo knows to
        look there is that it is spelled out here.

        Exactly one app is spelled out, and this method's name says so. The comment
        it replaced read "Wine prefix check (optional, if wechat/etc)", and the
        "etc" was never true -- no second app was ever handled and there is no
        general Wine-prefix mechanism here to extend. Adding one would mean
        deciding how to find a prefix from a name, which is the problem this line
        exists because nobody solved.
        """
        if "wechat" in app_name.lower():
            wine_prefix = home_path / ".xwechat"
            if wine_prefix.exists():
                found.add(wine_prefix)

    def _add_home_root_hidden_dirs(
        self,
        found: _ResiduePathSet,
        app_name: str,
        home_path: Path,
        pre_scanned_entries: dict[Path, _ResidueEntryIndex] | None,
    ) -> None:
        """Hidden directories at the home root whose name resembles the app's.

        Only HIDDEN dot-directories at the home root (e.g. ~/.someapp) are
        considered here. Visible top-level home folders are user workspaces and
        data -- ~/Projects, ~/IdeaProjects, ~/studio-projects, ~/notes-backup,
        ~/VirtualBox VMs -- and must NEVER be matched as residue: a fuzzy name
        hit there would permanently delete the user's own files. XDG user-data
        dirs are excluded too (defence in depth; they are visible anyway), and a
        name of three characters or fewer does not get a fuzzy pass at all.
        """
        if len(app_name) <= 3:
            return
        protected_dir_names = {data_dir.lower() for data_dir in LINUX_USER_DATA_DIRS}
        index = pre_scanned_entries.get(home_path) if pre_scanned_entries else None
        for entry_lower, entry_path in self._residue_candidates(
            index,
            {app_name.lower()},
            partial(self._scandir_entries, home_path, hidden_dirs_only=True),
        ):
            if entry_lower in protected_dir_names:
                continue
            if (
                self._name_matches(entry_lower, app_name.lower())
                and home_path in entry_path.parents
            ):
                found.add(entry_path)

    def _collateral_packages(self, app: AppRecord) -> list[str]:
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

        The apt query carries the same flags as the apt removal in
        execute_uninstall, `-s` in place of `-y`, so on Debian the floor is the
        transaction: what is listed here is what that removal takes.
        """
        app_id = str(app.get("id") or "")
        app_type = str(app.get("type") or "")
        if not app_id:
            return []
        if app_type == AppType.APT:
            # -s simulates without root; the whole transaction is narrated, and
            # the removal lines are the interesting ones.
            #
            # --autoremove is what puts the dependencies this removal orphans into
            # that transaction. Without it apt names them only in its "packages
            # were automatically installed and are no longer required" prose, with
            # no Remv/Purg prefix, so the preview omitted precisely the packages
            # the removal went on to take. Measured on debian:stable-slim:
            # `purge -s cowsay` narrates one Purg line, `purge --autoremove -s
            # cowsay` narrates eight.
            argv = ["apt-get", "purge", "--autoremove", "-s", app_id]
            env = system.APT_NONINTERACTIVE_ENV
        elif app_type == AppType.PACMAN:
            # --print-format implies --print, and --print is what makes pacman
            # skip the database lock it would otherwise need root for. %n asks
            # for bare names, so nothing here goes through a message catalog.
            argv = ["pacman", "-Rns", "--print-format", "%n", app_id]
            env = system.C_LOCALE_ENV
        elif app_type == AppType.DNF:
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
        elif app_type == AppType.ZYPPER:
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
        simulated = system.run_command(argv, capture=True, timeout=30, env=env)
        return self._parse_collateral(simulated.stdout, app_id, app_type)

    @staticmethod
    def _parse_collateral(stdout: str, app_id: str, app_type: str) -> list[str]:
        """Package names out of a simulated removal or a reverse-dependency reply."""
        names: list[str] = []
        for line in stdout.splitlines():
            entry = line.strip()
            if app_type == AppType.APT:
                # "Remv firefox [1:2snap1-0ubuntu2]", among Inst/Conf lines and
                # apt's own prose.
                fields = entry.split()
                if len(fields) < 2 or fields[0] not in ("Remv", "Purg"):
                    continue
                # A foreign-arch package is narrated qualified (libfoo:i386) while
                # the scan stripped the qualifier off its id, so without this the
                # app fails to recognise itself in its own transaction.
                entry = strip_package_arch(fields[1])
            # rpm reports "no package requires X" on stdout, and a package name
            # never holds whitespace, so this drops prose without matching on it.
            if not entry or len(entry.split()) != 1 or entry == app_id:
                continue
            if entry not in names:
                names.append(entry)
        return names

    def build_removal_targets(
        self, apps: list[AppRecord]
    ) -> list[tuple[AppRecord, list[Path], bool]]:
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

    def terminate_apps(self, targets: list[tuple[AppRecord, list[Path], bool]]) -> None:
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
            if app.get("type") == AppType.FLATPAK:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    system.run_command(
                        ["flatpak", "kill", str(app["id"])], capture=True, timeout=20
                    )
            for proc in self._candidate_process_names(app, paths):
                pattern = comm_pattern(proc)
                if pattern in running and pattern not in patterns:
                    patterns.append(pattern)
        self._terminate_process_patterns(patterns)

    @staticmethod
    def _flatpak_scope(app: AppRecord) -> str:
        """Which installation this Flatpak lives in, or "" when the scan could not tell.

        `flatpak list --columns=installation` prints "system", "user", or the id
        of a custom installation. The third answer normalises to "": its
        ownership is whatever the admin who created that installation decided,
        so the removal is left exactly as it was before any of this.
        """
        scope = str(app.get("flatpak_scope") or "").strip().lower()
        return scope if scope in ("system", "user") else ""

    @classmethod
    def flatpak_removal_needs_sudo(cls, app: AppRecord) -> bool:
        """Whether removing this Flatpak has to be root's work.

        A system-wide installation lives under /var/lib/flatpak, which the
        invoking user cannot write; flatpak falls back to asking polkit, and a
        session with no polkit agent -- ssh, a bare tty -- simply fails there.
        The screen calls this to decide whether to take a sudo session before it
        enters raw mode, and execute_uninstall calls it to build the command, so
        the authorization and the command that needs it cannot disagree.
        """
        return app.get("type") == AppType.FLATPAK and cls._flatpak_scope(app) == "system"

    def _terminate_app_processes(self, app: AppRecord, paths: list[Path]) -> None:
        """Close one app's processes, the step that has to precede its removal.

        terminate_apps applies the same policy to a whole selection and has
        normally already run by the time this does, which is what makes this
        cheap rather than redundant: the /proc pass finds nothing left and
        _terminate_process_patterns returns without waiting out a grace period.

        The two are near-twins on purpose. This one runs `flatpak kill` before
        taking the /proc snapshot and the batch one after, so an app that flatpak
        has already stopped costs a pkill and a 1.5 s wait there and nothing
        here. Merging them means choosing one of those two behaviours for both
        callers -- a decision to make deliberately, not while moving code.
        """
        # Use real executable names (id + .desktop Exec), never the localized
        # display name.
        all_process_names = self._candidate_process_names(app, paths)
        if app["type"] == AppType.FLATPAK:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                system.run_command(["flatpak", "kill", app["id"]], capture=True, timeout=20)

        # Patterns go through comm_pattern so a long executable name still
        # matches -- and still gets signalled. Which of them are actually
        # running is one /proc read for all of them; when terminate_apps has
        # already closed the selection this list comes back empty and the grace
        # periods are skipped entirely.
        running = running_process_comms()
        processes_to_kill: list[str] = []
        for proc in all_process_names:
            pattern = comm_pattern(proc)
            if pattern in running and pattern not in processes_to_kill:
                processes_to_kill.append(pattern)

        self._terminate_process_patterns(processes_to_kill)

    def _remove_package(self, app: AppRecord) -> system.CommandResult:
        """Run the one removal command this app's package manager needs.

        Eight package types plus an explicit refusal, and the branches are long
        because of the flags in them rather than the calls: apt-get instead of
        apt, zypper's --clean-deps, the Flatpak scope. Kept out of
        execute_uninstall so that its own subject -- the audit and history
        bookkeeping wrapped around this one call -- is not read through a hundred
        lines of package manager detail.

        Returns a CommandResult even where nothing is spawned (CLI, unsupported),
        because the caller's only question is whether the removal succeeded.
        """
        if app["type"] == AppType.FLATPAK:
            # The scope is not what makes the app findable -- `flatpak
            # uninstall` searches both installations to resolve a ref -- it
            # decides which copy goes when the same ref is installed in
            # both, and it names the installation the sudo decision was
            # made for.
            scope = self._flatpak_scope(app)
            flatpak_cmd = ["flatpak", "uninstall"]
            if scope:
                flatpak_cmd.append(f"--{scope}")
            flatpak_cmd += ["-y", app["id"]]
            return system.run_command(
                flatpak_cmd,
                use_sudo=self.flatpak_removal_needs_sudo(app),
                capture=True,
                timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
            )

        if app["type"] == AppType.SNAP:
            return system.run_command(
                ["snap", "remove", "--purge", app["id"]],
                use_sudo=True,
                capture=True,
                timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
            )

        if app["type"] == AppType.NPM:
            result = system.run_command(
                ["npm", "uninstall", "-g", app["id"]], capture=True, timeout=60
            )
            self._prune_empty_npm_scope_dir(app["id"])
            return result

        if app["type"] == AppType.CLI:
            # Remove standalone binary & install directory
            home_path = Path.home()
            cli_targets = [
                home_path / ".local/bin" / app["id"],
                home_path / ".local/share" / app["id"],
                home_path / f".{app['id']}",
            ]
            for cli_target in cli_targets:
                if cli_target.exists():
                    safe_remove(cli_target, use_trash=get_use_trash(), allow_app_data_removal=True)
            return system.CommandResult(
                args=["cli_uninstall"], returncode=0, stdout="CLI uninstalled"
            )

        if app["type"] == AppType.APT:
            # apt-get, not apt: apt prints "WARNING: apt does not have a stable
            # CLI interface" when its output is captured, and the rest of the
            # repository already standardises on apt-get.
            #
            # --autoremove for the same reason the zypper branch passes
            # --clean-deps, and with exactly the flags _collateral_packages()
            # simulated: the orphans this removal creates go with it, and they
            # are the ones the preview listed. The screen used to follow the
            # whole selection with a single system-wide `apt-get autoremove
            # --purge -y` instead, one transaction that took every unused
            # auto-installed package on the box -- none of them previewed, and
            # not only the ones this app had pulled in.
            return system.run_command(
                ["apt-get", "purge", "--autoremove", "-y", app["id"]],
                use_sudo=True,
                capture=True,
                env=system.APT_NONINTERACTIVE_ENV,
                timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
            )

        if app["type"] == AppType.PACMAN:
            return system.run_command(
                ["pacman", "-Rns", "--noconfirm", app["id"]],
                use_sudo=True,
                capture=True,
                timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
            )

        if app["type"] == AppType.ZYPPER:
            return system.run_command(
                # --clean-deps for the same reason the apt branch above passes
                # --autoremove: dnf drops the dependencies nothing needs any
                # more by default and pacman is asked to with -Rns, while
                # zypper keeps them unless told, which would leave openSUSE the
                # one family where uninstalling quietly leaves orphans on disk.
                ["zypper", "--non-interactive", "remove", "--clean-deps", app["id"]],
                use_sudo=True,
                capture=True,
                env=system.C_LOCALE_ENV,
                timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
            )

        if app["type"] == AppType.DNF:
            dnf_cmd = resolve_admin_tool(DNF)
            return system.run_command(
                [dnf_cmd, "remove", "-y", app["id"]],
                use_sudo=True,
                capture=True,
                timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
            )

        # Named explicitly rather than fallen through to: the old else ran
        # `dnf remove` for anything it did not recognise, which on a
        # zypper or an unlabelled entry meant a removal that could not
        # work. Failing says so; guessing a package manager does not.
        return system.CommandResult(
            args=["unsupported"],
            returncode=1,
            error=f"unsupported package type: {app['type']}",
        )

    def _prune_empty_npm_scope_dir(self, package_id: str) -> None:
        """Remove the @scope directory an npm uninstall leaves behind empty.

        `npm uninstall -g @cloudbase/cli` takes the package and leaves
        node_modules/@cloudbase, which nothing else will ever clean. Only removed
        when it is genuinely empty, so a second package under the same scope
        keeps it.
        """
        if "/" not in package_id:
            return
        scope = package_id.split("/")[0]
        npm_root = system.run_command(["npm", "root", "-g"], capture=True, timeout=5)
        if not (npm_root.ok and npm_root.stdout.strip()):
            return
        scope_dir = Path(npm_root.stdout.strip()) / scope
        if not scope_dir.is_dir():
            return
        with contextlib.suppress(OSError):
            if not any(scope_dir.iterdir()):
                scope_dir.rmdir()

    def _remove_residue_paths(self, paths: list[Path]) -> list[tuple[bool, str]]:
        """Delete an app's leftover data, reporting what happened to each path.

        Residue removal is recoverable (trash) rather than a permanent wipe:
        residue discovery is heuristic, so a mis-matched user directory must be
        undoable. config.json's use_trash=false is the one way to ask for an
        unrecoverable wipe instead. allow_app_data_removal still lets app-owned
        data go, while hard-protected paths (whitelist, credentials, system, XDG
        user-data dirs) stay blocked.

        Only ever called once the package itself is gone -- see the caller for
        why a failed removal leaves the data where it is.
        """
        removed_details: list[tuple[bool, str]] = []
        removed_systemd_service = False
        use_trash = get_use_trash()
        for residue_path in paths:
            success, _ = safe_remove(
                residue_path,
                use_trash=use_trash or _is_sandbox_app_data(residue_path),
                allow_app_data_removal=True,
            )
            path_text = str(residue_path)
            if success and path_text.endswith(".service") and ".config/systemd/user" in path_text:
                removed_systemd_service = True
            try:
                removed_details.append((success, str(residue_path.relative_to(Path.home()))))
            except ValueError:
                removed_details.append((success, path_text))

        if removed_systemd_service and shutil.which("systemctl"):
            system.run_command(["systemctl", "--user", "daemon-reload"], capture=True, timeout=10)

        return removed_details

    def execute_uninstall(self, app: AppRecord, paths: list[Path]):
        """Close one app's processes, remove its package, then remove its residue.

        In that order, and the last step only if the one before it succeeded. Each
        step is a call whose own name says what it does; what this method owns is
        the bookkeeping around them -- one audit event for the package removal, one
        history session that only reaches "ended" on the successful return.
        """
        app_name = str(app.get("name") or app.get("id") or "unknown")
        session_command = f"uninstall {app_name}"
        record_history_session(session_command, "started")
        package_status = "failed"
        package_event_recorded = False
        package_mode = str(app.get("type", "package")).lower()
        package_size = int(app.get("size_bytes") or 0)
        # Only the successful return below promotes this to "ended". Ctrl-C,
        # SIGTERM (which arrives as SystemExit) and a bug in the removal code all
        # leave it as it is, because `topo history` distinguishes an app whose
        # removal was cut short from one that finished with failures.
        session_status = "interrupted"

        try:
            self._terminate_app_processes(app, paths)

            removal = self._remove_package(app)
            package_status = "removed" if removal.ok else "failed"
            record_deletion_audit(app["id"], package_mode, package_status, package_size)
            package_event_recorded = True

            # Nothing is deleted while the app is still installed. The removal
            # above fails for reasons that have nothing to do with the data --
            # no polkit agent for a system-wide Flatpak, a lock held by another
            # package manager, a package the type dispatch does not know -- and
            # deleting the configuration of an app that is still there is the
            # worst of both outcomes: the user has an installed app that has
            # forgotten everything, and a retry cannot bring it back. Leaving
            # the paths alone makes the failure retryable.
            data_left_in_place = bool(paths) and package_status != "removed"
            removed_details: list[tuple[bool, str]] = []
            if package_status == "removed":
                removed_details = self._remove_residue_paths(paths)

            session_status = "ended"
            return {
                "package_removed": package_status == "removed",
                "removed_paths": removed_details,
                "data_left_in_place": data_left_in_place,
            }
        finally:
            if package_status == "failed" and not package_event_recorded:
                record_deletion_audit(app.get("id", app_name), package_mode, "failed", package_size)
            record_history_session(session_command, session_status)
