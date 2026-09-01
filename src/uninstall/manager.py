"""The uninstall scan: every installed app, its size, and its removal preview.

What is left of ``UninstallManager`` after R1's split is the part that has state.
The two halves it composes are stateless module functions -- ``discovery`` finds
the packages, ``residue`` finds the per-user paths, ``collateral`` asks what else
would go -- and this class holds the three things they cannot: the class-level
scan cache that lets a second screen reuse the first one's results, the residue
index :func:`~src.uninstall.residue.pre_scan_search_roots` built during the scan,
and ``self.apps``.

Both public methods spend that state rather than recomputing it, which is the
reason it is held at all: ``build_removal_targets`` reuses the index from
``run_full_scan`` so the preview walks no directory twice and cannot disagree
with the sizes the user picked a row by.

Nothing here deletes -- :mod:`src.uninstall.removal` does, and does not appear
in this module's imports.
"""

import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..core import system
from ..core.file_ops import (
    bytes_to_human,
    comm_pattern,
    get_size_fast,
    running_process_comms,
)
from ..core.package_manager import PACKAGE_QUERY_TOOLS
from . import processes, residue
from .collateral import collateral_packages
from .discovery import AppRecord, discover_installed_apps


class UninstallManager:
    _SCAN_CACHE_TTL_SECONDS = 30
    _scan_cache_apps: list[AppRecord] | None = None
    _scan_cache_time = 0.0
    _scan_cache_key: tuple[Any, ...] | None = None

    def __init__(self):
        self.apps: list[AppRecord] = []
        # The residue index run_full_scan built, kept so the preview can reuse it.
        # Only ever valid inside the process that built it: ResidueEntryIndex
        # buckets grams with the per-process-randomized str.__hash__.
        self._pre_scanned_entries: dict[Path, residue.ResidueEntryIndex] | None = None

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

    def _calculate_app_sizes_and_residues(
        self, apps: list[AppRecord], pre_scanned_entries: dict[Path, residue.ResidueEntryIndex]
    ) -> None:
        """Calculates total app sizes including user data and cache residues in parallel."""

        def _process_single_app(app: AppRecord):
            residue_paths = residue.find_residue_paths(
                app["id"], app["name"], pre_scanned_entries=pre_scanned_entries
            )
            # Both sides are already Path -- install_dir by AppRecord's
            # annotation, residue_paths by find_residue_paths' return type -- so
            # only resolve() does any work here. It stays on both sides because
            # it is what makes the comparison mean "the same directory": the
            # install dir is chosen from ~/.local/share, ~/.config or ~/.<name>,
            # any of which can be reached through a symlink that the residue scan
            # names differently.
            inst_dir_val = app.get("install_dir")
            target_inst_dir: Path | None = inst_dir_val.resolve() if inst_dir_val else None
            filtered_residue_paths = [
                p
                for p in residue_paths
                if target_inst_dir is None or p.resolve() != target_inst_dir
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

        pre_scanned_entries = residue.pre_scan_search_roots()
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
                for app, collateral in zip(apps, pool.map(collateral_packages, apps), strict=True):
                    app["collateral_packages"] = collateral
        targets = []
        for app in apps:
            app_paths = residue.find_residue_paths(
                app["id"], app["name"], pre_scanned_entries=self._pre_scanned_entries
            )
            is_running = any(
                comm_pattern(proc) in running
                for proc in processes.candidate_process_names(app, app_paths)
            )
            targets.append((app, app_paths, is_running))
        return targets
