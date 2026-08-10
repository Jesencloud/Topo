import os
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..core.config import get_purge_paths
from ..core.constants import (
    CLEAR_SCREEN,
    CYAN,
    MONOREPO_INDICATORS,
    PROJECT_INDICATORS,
    PURGE_TARGETS,
    RESET,
    THEME_TITLE,
)
from ..core.file_ops import bytes_to_human, get_size_fast, safe_remove
from ..core.scan_cache import ScanCache
from ..ui.navigator import Navigator, PaginatedSelector


class Scanner:
    def __init__(self, search_paths: list[str]):
        self.search_paths = [Path(p).expanduser().resolve() for p in search_paths]
        self.found_projects: set[Path] = set()
        self.found_artifacts: list[Path] = []

    def scan_for_projects(self, max_depth: int = 4) -> Iterator[Path]:
        """Discovers project roots within search paths."""
        for root in self.search_paths:
            if not root.is_dir():
                continue

            yield from self._recursive_scan(root, 0, max_depth)

    def _recursive_scan(self, path: Path, depth: int, max_depth: int) -> Iterator[Path]:
        """Iterative (stack-based) non-recursive helper for project discovery."""
        stack: list[tuple[Path, int]] = [(path, depth)]
        while stack:
            curr_path, curr_depth = stack.pop()
            if curr_depth > max_depth:
                continue

            try:
                with os.scandir(curr_path) as it:
                    entries = list(it)
            except OSError:
                continue

            is_root = any(
                entry.name in MONOREPO_INDICATORS or entry.name in PROJECT_INDICATORS
                for entry in entries
            )
            if is_root:
                yield curr_path

            if curr_depth < max_depth:
                for entry in reversed(entries):
                    if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                        stack.append((Path(entry.path), curr_depth + 1))

    def scan_artifacts(self, project_path: Path) -> list[Path]:
        """Finds heavy artifacts within a discovered project root."""
        artifacts: list[Path] = []
        try:
            with os.scandir(project_path) as it:
                entries = list(it)
        except OSError:
            return artifacts
        # "bin" is a build-output directory only for .NET projects; without this
        # guard any project's bin/ (shell scripts, vendored binaries) would be
        # treated as purgeable. Require a .NET project file alongside it.
        has_dotnet_project = any(
            entry.name.endswith((".csproj", ".sln", ".fsproj", ".vbproj")) for entry in entries
        )
        for entry in entries:
            if not (entry.is_dir() and entry.name in PURGE_TARGETS):
                continue
            if entry.name == "bin" and not has_dotnet_project:
                continue
            artifacts.append(Path(entry.path))
        return artifacts


class PurgeManager:
    def __init__(self):
        self.scanner = Scanner(get_purge_paths())
        self.results: list[dict[str, Any]] = []

    def run_scan(self):
        """Orchestrates the scanning process."""
        print("🔍 Scanning for projects and heavy artifacts...")

        # 1. Discover projects
        projects = list(self.scanner.scan_for_projects())

        # 2. Find artifacts in projects
        all_artifacts = []
        seen_artifacts: set[str] = set()
        for project in projects:
            artifacts = self.scanner.scan_artifacts(project)
            for artifact in artifacts:
                try:
                    key = str(artifact.resolve())
                except OSError:
                    key = str(artifact)
                if key not in seen_artifacts:
                    seen_artifacts.add(key)
                    all_artifacts.append(artifact)

        if not all_artifacts:
            return []

        # 3. Calculate sizes in parallel
        print(f"📊 Found {len(all_artifacts)} potential targets. Calculating sizes...")
        results = []

        def get_item_info(p):
            size = get_size_fast(p)
            if size > 0:
                return {
                    "project": p.parent.name,
                    "path": p,
                    "size": size,
                    "human_size": bytes_to_human(size),
                }
            return None

        with ThreadPoolExecutor(max_workers=8) as executor:
            infos = list(executor.map(get_item_info, all_artifacts))

        results = [i for i in infos if i]
        self.results = sorted(results, key=lambda x: x["size"], reverse=True)
        return self.results

    def execute_purge(self, selected_indices: list[int]):
        """Removes the selected artifact directories."""
        total_freed = 0
        count = 0
        for idx in selected_indices:
            item = self.results[idx]
            size = item["size"]
            success, _ = safe_remove(item["path"], use_trash=False)
            if success:
                total_freed += size
                count += 1
        return count, bytes_to_human(total_freed)


def run_purge(dry_run=False):
    while True:
        print(f"{THEME_TITLE}➤ Project Purge{RESET}")
        manager = PurgeManager()
        results = manager.run_scan()

        if not results:
            print("✨ No heavy artifacts found. Your projects are clean!")
            Navigator.wait_for_return()
            return

        selector = PaginatedSelector("Select Project Artifacts to Purge", results)
        action = selector.run()

        if action == "MANAGE_PATHS":
            from ..core.config import add_purge_path, get_purge_paths, remove_purge_path

            while True:
                sys.stdout.write(CLEAR_SCREEN)
                sys.stdout.flush()
                print(f"\n{CYAN}⚙️  topo Purge Settings{RESET}")
                print()
                paths = get_purge_paths()
                print("Current Purge Search Paths:")
                for i, p in enumerate(paths):
                    print(f"  [{i + 1}] {p}")

                print("\nOptions: [A] Add Path | [R] Remove Path | [B] Back to Scan")
                c = input("➤ ").lower()
                if c == "a":
                    new_p = input("Enter new search path: ")
                    if add_purge_path(new_p):
                        print(f"✅ Added: {new_p}")
                    input("\nPress Enter...")
                elif c == "r":
                    try:
                        idx = int(input("Enter index to remove: ")) - 1
                        if 0 <= idx < len(paths):
                            remove_purge_path(paths[idx])
                            print("✅ Removed path.")
                        else:
                            print("❌ Invalid index.")
                    except ValueError:
                        print("❌ Invalid input.")
                    input("\nPress Enter...")
                elif c == "b":
                    break
            continue  # Re-scan with new paths

        if action and isinstance(action, list):
            selected = action
            if dry_run:
                total_size = sum(results[i]["size"] for i in selected)
                print(
                    f"\n🧪 [DRY RUN] Would remove {len(selected)} items, freeing {bytes_to_human(total_size)}"
                )
            else:
                count, total_freed = manager.execute_purge(selected)
                if count > 0:
                    ScanCache.clear()
                print(f"\n✨ Purge complete: {count} items removed, {total_freed} space freed.")
            if not Navigator.wait_for_return():
                break
            continue
        else:
            break
