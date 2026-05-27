import os
import time
from pathlib import Path
from typing import List, Set, Iterator, Dict, Any
from concurrent.futures import ThreadPoolExecutor
from ..core.constants import PROJECT_INDICATORS, MONOREPO_INDICATORS, PURGE_TARGETS
from ..core.file_ops import get_size, safe_remove, bytes_to_human
from ..core.config import get_purge_paths

class Scanner:
    def __init__(self, search_paths: List[str]):
        self.search_paths = [Path(p).expanduser().resolve() for p in search_paths]
        self.found_projects: Set[Path] = set()
        self.found_artifacts: List[Path] = []

    def is_project_root(self, path: Path) -> bool:
        """Checks if a directory is a project root based on indicators."""
        try:
            for entry in os.scandir(path):
                if entry.name in MONOREPO_INDICATORS or entry.name in PROJECT_INDICATORS:
                    return True
        except OSError:
            pass
        return False

    def scan_for_projects(self, max_depth: int = 4) -> Iterator[Path]:
        """Discovers project roots within search paths."""
        for root in self.search_paths:
            if not root.is_dir():
                continue
            
            yield from self._recursive_scan(root, 0, max_depth)

    def _recursive_scan(self, path: Path, depth: int, max_depth: int) -> Iterator[Path]:
        """Recursive helper for project discovery."""
        if depth > max_depth:
            return

        if self.is_project_root(path):
            yield path

        try:
            with os.scandir(path) as it:
                for entry in it:
                    if entry.is_dir() and not entry.name.startswith('.'):
                        yield from self._recursive_scan(Path(entry.path), depth + 1, max_depth)
        except OSError:
            pass

    def scan_artifacts(self, project_path: Path) -> List[Path]:
        """Finds heavy artifacts within a discovered project root."""
        artifacts = []
        try:
            with os.scandir(project_path) as it:
                for entry in it:
                    if entry.is_dir() and entry.name in PURGE_TARGETS:
                        artifacts.append(Path(entry.path))
        except OSError:
            pass
        return artifacts

from ..ui.navigator import PaginatedSelector
from ..core.analyze import ScanCache

def run_purge(dry_run=False):
    while True:
        print("\033[1;95m➤ Project Purge\033[0m")
        manager = PurgeManager()
        results = manager.run_scan()
        
        if not results:
            print("✨ No heavy artifacts found. Your projects are clean!")
            input("\nPress Enter to return to menu...")
            return

        selector = PaginatedSelector("Select Project Artifacts to Purge", results)
        action = selector.run()
        
        if action == "MANAGE_PATHS":
            from .core.config import add_purge_path, remove_purge_path, get_purge_paths
            while True:
                os.system('clear')
                print("\n\033[1;36m⚙️  topo Purge Settings\033[0m")
                print("-" * 50)
                paths = get_purge_paths()
                print(f"Current Purge Search Paths:")
                for i, p in enumerate(paths):
                    print(f"  [{i+1}] {p}")
                
                print("\nOptions: [A] Add Path | [R] Remove Path | [B] Back to Scan")
                c = input("➤ ").lower()
                if c == 'a':
                    new_p = input("Enter new search path: ")
                    if add_purge_path(new_p): print(f"✅ Added: {new_p}")
                    input("\nPress Enter...")
                elif c == 'r':
                    try:
                        idx = int(input("Enter index to remove: ")) - 1
                        if 0 <= idx < len(paths):
                            remove_purge_path(paths[idx])
                            print(f"✅ Removed path.")
                        else: print("❌ Invalid index.")
                    except: print("❌ Invalid input.")
                    input("\nPress Enter...")
                elif c == 'b':
                    break
            continue # Re-scan with new paths

        if action and isinstance(action, list):
            selected = action
            if dry_run:
                total_size = sum(results[i]['size'] for i in selected)
                from .core.file_ops import bytes_to_human
                print(f"\n🧪 [DRY RUN] Would remove {len(selected)} items, freeing {bytes_to_human(total_size)}")
            else:
                count, total_freed = manager.execute_purge(selected)
                if count > 0: ScanCache.clear()
                print(f"\n✨ Purge complete: {count} items removed, {total_freed} space freed.")
            input("\nPress Enter to return to menu...")
            break
        else:
            break

