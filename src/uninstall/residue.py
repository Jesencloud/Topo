"""Which per-user paths belong to one app -- and the index that makes asking cheap.

Residue is everything an app leaves outside its package: ``~/.config/<name>``,
a stale ``.desktop`` launcher, icons in every theme directory, a systemd user
unit that would keep restarting a program that is no longer installed. Nothing
records which of those belong to which app, so every answer here is derived from
names, and the whole module is written around that being a guess: two gates
narrow it (:func:`_residue_name_targets` decides which spellings of the app's own
name count, :func:`~src.uninstall.names.name_matches` decides what a directory
name is allowed to match), and a visible folder in the home directory is never a
candidate no matter how well it matches.

Nothing here deletes, opens, or renames anything -- :mod:`src.uninstall.removal`
does that, with this module's list as its input. That is the one-way arrow worth
keeping: a guess is allowed to be generous only as long as the code making it
cannot act on it.

:class:`ResidueEntryIndex` is the reason a full scan is affordable. Twenty-seven
apps each searching six roots plus a full recursion of two icon trees is the same
directory walk twenty-seven times over; ``run_full_scan`` walks each root once,
builds an index, and hands it to every app. The index only ever narrows the
candidate list -- every pair that survives it is still put through
``name_matches`` -- so it cannot change an answer, only the time it takes.

Split out of ``UninstallManager``, where this was two thirds of the class and sat
between the scan cache and the removal it feeds. Nothing here may import
``manager.py``: the scan calls in this direction, and so does the preview.
"""

import contextlib
import os
from array import array
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from ..core.desktop_entry import get_desktop_exec_names, get_desktop_icon
from ..core.whitelist import LINUX_USER_DATA_DIRS
from .discovery import app_text
from .names import GENERIC_TOKENS, name_matches

# Apps whose configuration is a system's security posture rather than one user's
# preferences: a password store, a VPN profile, an ssh or gnupg key, an input
# method the desktop needs to stay typable. _requires_official_only_uninstall
# turns a token match here into an empty residue list, so topo removes the
# package and leaves the data to whoever installed it.
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


@dataclass(frozen=True, eq=False)
class ResidueEntryIndex:
    """One scanned root plus compact lookup tables for residue candidates.

    ``candidates()`` narrows a root's entries down to the ones that could possibly
    satisfy :func:`~src.uninstall.names.name_matches`; every survivor is still run
    through that function, so the index can only ever cost extra work, never invent
    a match. Each lookup table exists to cover exactly one ``name_matches``
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
        # ``manager.py``'s run_full_scan() and consumes it in the
        # ThreadPoolExecutor right after, which shares the interpreter and so
        # shares the hash seed.
        return hash(gram) % cls._GRAM_BUCKET_COUNT

    @classmethod
    def build(cls, entries: list[tuple[str, Path]]) -> "ResidueEntryIndex":
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
            if not target or target in GENERIC_TOKENS:
                continue
            candidate_ids.update(self.exact.get(target, ()))
            if len(target) >= 3:
                candidate_ids.update(self.prefixes.get(target[:3], ()))
            if len(target) >= 5:
                candidate_ids.update(self.gram_buckets[self._gram_bucket(target[:5])])
        return [self.entries[index] for index in sorted(candidate_ids)]


class _ResiduePathSet:
    """The residue paths found for one app, in discovery order, without duplicates.

    Every step of :func:`find_residue_paths` adds through here,
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


def _requires_official_only_uninstall(app_id: str, app_name: str) -> bool:
    text = app_text(app_id, app_name)
    return any(token in text for token in _OFFICIAL_ONLY_TOKENS)


def _get_app_keywords(desktop_file: Path) -> list[str]:
    """Extracts potential folder name keywords from Exec and Icon fields."""
    keywords = {name.lower() for name in get_desktop_exec_names(desktop_file)}
    icon_name = get_desktop_icon(desktop_file).lower()
    if icon_name:
        keywords.add(icon_name)
    return list(keywords)


def pre_scan_search_roots() -> dict[Path, ResidueEntryIndex]:
    """Scan shared roots once and build reusable residue-candidate indexes.

    The roots are the ones find_residue_paths() searches, taken from the same
    two helpers it takes them from, so neither side can grow a root the other
    does not know about.
    """
    home_path = Path.home()
    pre_scanned_entries: dict[Path, ResidueEntryIndex] = {}
    for root in _residue_search_roots(home_path):
        if root.exists():
            try:
                entries: list[tuple[str, Path]] = []
                with os.scandir(root) as it:
                    for item_entry in it:
                        entries.append((item_entry.name.lower(), Path(item_entry.path)))
                pre_scanned_entries[root] = ResidueEntryIndex.build(entries)
            except OSError:
                pass
    for icon_root in _residue_icon_roots(home_path):
        if not icon_root.exists():
            continue
        entries = []
        with contextlib.suppress(OSError):
            for icon_file in icon_root.rglob("*"):
                if icon_file.is_file():
                    entries.append((icon_file.name.lower(), icon_file))
        pre_scanned_entries[icon_root] = ResidueEntryIndex.build(entries)

    service_root = home_path / ".config/systemd/user"
    if service_root.exists():
        with contextlib.suppress(OSError):
            pre_scanned_entries[service_root] = ResidueEntryIndex.build(
                [(service.name.lower(), service) for service in service_root.glob("*.service")]
            )

    with contextlib.suppress(OSError), os.scandir(home_path) as home_entries:
        pre_scanned_entries[home_path] = ResidueEntryIndex.build(
            [
                (entry.name.lower(), Path(entry.path))
                for entry in home_entries
                if entry.is_dir(follow_symlinks=False) and entry.name.startswith(".")
            ]
        )
    return pre_scanned_entries


def find_residue_paths(
    app_id: str,
    app_name: str,
    pre_scanned_entries: dict[Path, ResidueEntryIndex] | None = None,
) -> list[Path]:
    """Every per-user path that belongs to one app: config, cache, data, launcher.

    This list is what the removal screen previews and what
    :func:`~src.uninstall.removal.execute_uninstall` then deletes, so each step
    below is deliberately narrow: see :func:`~src.uninstall.names.name_matches`
    for what "named after the app" is allowed to mean, and
    :func:`_add_home_root_hidden_dirs` for why a visible folder in the home
    directory is never a candidate.

    Two things about the order. Every step adds through one
    :class:`_ResiduePathSet`, so the order they run in is the order the preview
    lists paths in. And ``_add_home_dot_dirs`` runs against the name variants
    *before* the launcher keywords widen them: moving the keyword step above it
    would let a ``~/.<Exec name>`` directory be found too, which is a change in
    what gets deleted rather than a reordering, and is left alone here.
    """
    if _requires_official_only_uninstall(app_id, app_name):
        return []

    home_path = Path.home()
    found = _ResiduePathSet()
    targets = _residue_name_targets(app_id, app_name)
    _add_home_dot_dirs(found, home_path, targets)
    targets |= _desktop_launcher_keywords(app_id, home_path)
    _add_app_data_dirs(found, home_path, targets, pre_scanned_entries)
    _add_launcher_and_icons(found, app_id, home_path, targets, pre_scanned_entries)
    _add_systemd_user_services(found, home_path, targets, pre_scanned_entries)
    _add_hardcoded_wine_prefix(found, app_name, home_path)
    _add_home_root_hidden_dirs(found, app_name, home_path, pre_scanned_entries)
    return found.as_list()


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


def _residue_icon_roots(home_path: Path) -> list[Path]:
    """Where a user-installed app drops its icons. Shared with the pre-scan."""
    return [
        home_path / ".local/share/icons",
        home_path / ".local/share/pixmaps",
    ]


def _residue_name_targets(app_id: str, app_name: str) -> set[str]:
    """The names a directory belonging to this app could plausibly be spelled with.

    An id is often not what the app is called on disk: the last segment of a
    Flatpak id (``org.gnome.Music`` -> ``music``) and the parts of an npm scope
    (``@scope/pkg``) both name the same app, and a ``-``/``_`` suffix is
    frequently absent from the directory (``code-insiders`` -> ``code``). The
    suffix-stripped prefix is only kept when it is at least three characters
    and not one of the generic tokens, because a target like ``data`` would go
    on to match unrelated directories -- ``name_matches`` rejects those tokens
    as well, so this is the first of two gates rather than the only one.
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
                if len(prefix) >= 3 and prefix not in GENERIC_TOKENS:
                    targets.add(prefix)
    return targets


def _desktop_launcher_keywords(app_id: str, home_path: Path) -> set[str]:
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
            keywords.update(_get_app_keywords(desktop_file))
    return keywords


def _add_home_dot_dirs(found: _ResiduePathSet, home_path: Path, targets: set[str]) -> None:
    """``~/.<name>`` directories, which no root below covers.

    A tool that predates XDG keeps its state straight in the home directory:
    ~/.claude, ~/.kimi, ~/.codex, ~/.grok, ~/.cloudbase. Matched exactly, not
    fuzzily -- :func:`_add_home_root_hidden_dirs` is the fuzzy pass over the
    same directory, and it carries the guards that a fuzzy match at the home
    root needs.
    """
    for target in targets:
        if len(target) >= 3:
            dot_dir = home_path / f".{target}"
            if dot_dir.is_dir():
                found.add(dot_dir)


def _residue_candidates(
    index: ResidueEntryIndex | None,
    targets: set[str],
    list_entries: Callable[[], list[tuple[str, Path]]],
) -> list[tuple[str, Path]]:
    """The ``(lowercased name, path)`` pairs worth matching in one root.

    ``run_full_scan`` scans the shared roots once and hands every app the
    resulting index; a single ``topo uninstall <app>`` has no index and lists
    the root here instead. Both arms return a list, and every pair either arm
    returns is still put through ``name_matches``, so an index can only narrow
    the work, never decide the answer.

    One shape for all three indexed roots. They used to be written three
    different ways -- two ended in ``for ... in indexed_x or []`` and the third
    did not -- and since neither fallback could produce None, the two ``or []``
    were dead code that made the third site look like the one with the bug.
    """
    if index is not None:
        return index.candidates(targets)
    return list_entries()


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


def _named_entries(paths: Iterable[Path]) -> list[tuple[str, Path]]:
    """A listing's results as ``(lowercased name, path)``, best effort.

    Same partial-result contract as :func:`_scandir_entries`.
    """
    entries: list[tuple[str, Path]] = []
    with contextlib.suppress(OSError):
        for path in paths:
            entries.append((path.name.lower(), path))
    return entries


def _icon_file_entries(icon_root: Path) -> list[tuple[str, Path]]:
    """Every file under one icon root, at any depth: icons sit in theme/size subdirs.

    A function rather than an expression at the call site so that the walk starts
    only when there is no index to answer from -- the leading iterable of a
    generator expression is evaluated where it is written, which would have
    called rglob() even on the indexed path.
    """
    return _named_entries(icon for icon in icon_root.rglob("*") if icon.is_file())


def _service_file_entries(service_root: Path) -> list[tuple[str, Path]]:
    """The unit files in ~/.config/systemd/user; flat, so not recursive."""
    return _named_entries(service_root.glob("*.service"))


def _add_app_data_dirs(
    found: _ResiduePathSet,
    home_path: Path,
    targets: set[str],
    pre_scanned_entries: dict[Path, ResidueEntryIndex] | None,
) -> None:
    """Directories named after the app in the XDG and sandbox roots."""
    for root in _residue_search_roots(home_path):
        if not root.exists():
            continue
        index = pre_scanned_entries.get(root) if pre_scanned_entries else None
        for entry_lower, entry_path in _residue_candidates(
            index, targets, partial(_scandir_entries, root)
        ):
            if any(name_matches(entry_lower, target) for target in targets):
                found.add(entry_path)


def _add_launcher_and_icons(
    found: _ResiduePathSet,
    app_id: str,
    home_path: Path,
    targets: set[str],
    pre_scanned_entries: dict[Path, ResidueEntryIndex] | None,
) -> None:
    """The app's user-level .desktop launcher, and icon files named after it.

    Left behind, these are what keeps a removed app in the application grid
    with its icon intact.
    """
    local_desktop = home_path / ".local/share/applications" / f"{app_id}.desktop"
    if local_desktop.exists():
        found.add(local_desktop)

    for icon_root in _residue_icon_roots(home_path):
        if not icon_root.exists():
            continue
        index = pre_scanned_entries.get(icon_root) if pre_scanned_entries else None
        for file_lower, icon_file in _residue_candidates(
            index, targets, partial(_icon_file_entries, icon_root)
        ):
            if any(name_matches(file_lower, target) for target in targets):
                found.add(icon_file)


def _add_systemd_user_services(
    found: _ResiduePathSet,
    home_path: Path,
    targets: set[str],
    pre_scanned_entries: dict[Path, ResidueEntryIndex] | None,
) -> None:
    """User units named after the app, which would otherwise keep starting it."""
    service_root = home_path / ".config/systemd/user"
    if not service_root.exists():
        return
    index = pre_scanned_entries.get(service_root) if pre_scanned_entries else None
    for file_lower, service_file in _residue_candidates(
        index, targets, partial(_service_file_entries, service_root)
    ):
        if any(name_matches(file_lower, target) for target in targets):
            found.add(service_file)


def _add_hardcoded_wine_prefix(found: _ResiduePathSet, app_name: str, home_path: Path) -> None:
    """WeChat's Wine prefix -- the one path here that is written out, not derived.

    Every other step matches names the app itself supplies (its id, its display
    name, its launcher's Exec and Icon fields) against what is on disk.
    ``~/.xwechat`` is none of those names: the prefix is created by the vendor's
    own launcher under a name of its choosing, so the only reason topo knows to
    look there is that it is spelled out here.

    Exactly one app is spelled out, and this function's name says so. The comment
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
    found: _ResiduePathSet,
    app_name: str,
    home_path: Path,
    pre_scanned_entries: dict[Path, ResidueEntryIndex] | None,
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
    for entry_lower, entry_path in _residue_candidates(
        index,
        {app_name.lower()},
        partial(_scandir_entries, home_path, hidden_dirs_only=True),
    ):
        if entry_lower in protected_dir_names:
            continue
        if name_matches(entry_lower, app_name.lower()) and home_path in entry_path.parents:
            found.add(entry_path)
