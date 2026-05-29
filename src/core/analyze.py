import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ..ui.navigator import AnalyzeSelector, ConfirmSelector, TopFilesSelector
from ..ui.tui import show_banner
from .constants import BLUE, CYAN, MAGENTA, YELLOW
from .file_ops import bytes_to_human, get_size, safe_remove


# --- Internal Cache System ---
class ScanCache:
    """Memory-only cache for rust-engine scan results to make back navigation instant."""

    _data: dict[str, Any] = {}

    @classmethod
    def get(cls, path: Path) -> dict[str, Any] | None:
        return cls._data.get(str(path))

    @classmethod
    def set(cls, path: Path, data: dict[str, Any]):
        cls._data[str(path)] = data

    @classmethod
    def clear(cls):
        cls._data = {}


def get_rust_scan_data(path: Path) -> dict[str, Any]:
    """Calls the topo-core-x86_64 binary and returns parsed JSON."""
    binary = Path(__file__).parent / "bin" / "topo-core-x86_64"
    if not binary.exists():
        return None

    # Check cache first
    cached = ScanCache.get(path)
    if cached:
        return cached

    try:
        res = subprocess.run([str(binary), str(path)], capture_output=True, text=True)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            ScanCache.set(path, data)
            return data
    except Exception:
        pass
    return None


def get_age_hint(path: Path) -> str:
    """Returns a rough age hint like >90d, >6mo, >1y based on mtime."""
    try:
        mtime = path.stat().st_mtime
        days = (time.time() - mtime) / 86400
        if days < 30:
            return ""
        if days > 365:
            return f">{int(days / 365)}y"
        if days > 30:
            return f">{int(days / 30)}mo"
        return f">{int(days)}d"
    except Exception:
        return ""


def get_old_items_info(dir_path: Path, days_threshold: int = 90) -> list[dict[str, Any]]:
    """Returns a list of items in a directory older than X days."""
    old_items = []
    cutoff = time.time() - (days_threshold * 86400)
    try:
        for item in dir_path.iterdir():
            try:
                stat = item.stat()
                if stat.st_mtime < cutoff:
                    old_items.append(
                        {
                            "name": item.name,
                            "path": item,
                            "size": get_size(item),
                            "mtime": stat.st_mtime,
                        }
                    )
            except Exception:
                continue
    except Exception:
        pass
    return sorted(old_items, key=lambda x: x["size"], reverse=True)


def run_deep_analysis(target_path: Path = None):
    # State Stack stores: {"target": Path, "results": [], "data": {}, "total_size": int}
    state_stack = []

    # Current active state
    current_target = target_path
    results = []
    data = None
    total_scan_size = 0
    needs_scan = True

    while True:
        target_to_scan = current_target or Path.home()
        view_title = "Analyze Disk" if current_target is None else f"Exploring: {current_target}"

        if needs_scan:
            msg = f"   🚀 Rust Engine: Intelligence Scan on {target_to_scan.name if current_target else 'Home'}..."
            print(msg, end="\r")
            data = get_rust_scan_data(target_to_scan)
            if not data:
                print("\n   ❌ Engine scan failed.")
                time.sleep(1.5)
                if state_stack:
                    prev = state_stack.pop()
                    current_target = prev["target"]
                    results = prev["results"]
                    data = prev["data"]
                    total_scan_size = prev["total_size"]
                    needs_scan = False
                    continue
                else:
                    break

            total_scan_size = data.get("total_size_bytes", 0)
            results = []

            if current_target is None:
                # Root View: Standard Categories
                total_used = shutil.disk_usage("/").used
                targets = [
                    {"name": "Home", "path": Path.home(), "color": CYAN},
                    {
                        "name": "Applications",
                        "path": Path("/usr/share/applications"),
                        "color": MAGENTA,
                    },
                    {"name": "System", "path": Path("/usr"), "color": BLUE},
                ]
                for t in targets:
                    if t["path"].exists():
                        if t["path"] == Path.home():
                            size = total_scan_size
                        elif str(t["path"]) == "/":
                            size = total_used
                        else:
                            t_data = get_rust_scan_data(t["path"])
                            size = t_data.get("total_size_bytes", 0) if t_data else 0
                        results.append(
                            {
                                "name": t["name"],
                                "path": t["path"],
                                "size": size,
                                "percent": (size / total_used) * 100,
                                "color": t["color"],
                                "icon": "📊" if str(t["path"]) == "/" else "📁",
                                "age_hint": get_age_hint(t["path"]),
                            }
                        )

                # --- LINUX INSIGHTS: Detect hidden space killers ---
                print("   🔍 Analyzing Linux Insights...", end="\r")
                home = Path.home()
                insights = [
                    {"name": "Old Downloads (90d+)", "path": home / "Downloads", "is_smart": True},
                    {"name": "Docker Data", "path": home / ".docker"},
                    {"name": "Docker System", "path": Path("/var/lib/docker")},
                    {"name": "Apt Cache", "path": Path("/var/cache/apt/archives")},
                    {"name": "Pacman Cache", "path": Path("/var/cache/pacman/pkg")},
                    {"name": "Dnf Cache", "path": Path("/var/cache/dnf")},
                    {"name": "Snap Data", "path": home / "snap"},
                    {"name": "Flatpak Data", "path": home / ".local/share/flatpak"},
                    {"name": "Ollama Models", "path": home / ".ollama" / "models"},
                ]

                for ins in insights:
                    p = ins["path"]
                    if p.exists():
                        size = 0
                        smart_items = []

                        if ins.get("is_smart"):
                            # For smart views, we pre-calculate filtered items
                            smart_items = get_old_items_info(p)
                            size = sum(item["size"] for item in smart_items)
                        else:
                            ins_data = get_rust_scan_data(p)
                            size = ins_data.get("total_size_bytes", 0) if ins_data else 0

                        if size > 10 * 1024 * 1024:  # Only show if > 10MB to keep Root clean
                            results.append(
                                {
                                    "name": ins["name"],
                                    "path": p,
                                    "size": size,
                                    "percent": (size / total_used) * 100,
                                    "color": YELLOW,
                                    "icon": "👀",
                                    "age_hint": get_age_hint(p),
                                    "is_smart": ins.get("is_smart"),
                                    "smart_items": smart_items,
                                }
                            )
            else:
                total_path_size = total_scan_size or 1
                subdir_map = data.get("subdirs", {})
                for name, size in subdir_map.items():
                    if name.startswith("."):
                        continue
                    full_path = current_target / name
                    icon = "📁" if full_path.is_dir() else "📄"
                    results.append(
                        {
                            "name": name,
                            "path": full_path,
                            "size": size,
                            "percent": (size / total_path_size) * 100,
                            "color": CYAN,
                            "icon": icon,
                            "age_hint": get_age_hint(full_path),
                        }
                    )
                results.sort(key=lambda x: x["size"], reverse=True)
                results = results[:50]
            needs_scan = False

        selector = AnalyzeSelector(
            view_title,
            results,
            show_banner=show_banner if current_target is None else None,
            can_select=(current_target is not None),
        )
        action, idx = selector.run()

        if action == "QUIT":
            break
        elif action == "BACK":
            if state_stack:
                prev = state_stack.pop()
                current_target = prev["target"]
                results = prev["results"]
                data = prev["data"]
                total_scan_size = prev["total_size"]
                # Recalculate parent percentages to reflect any deletions done in child
                if total_scan_size > 0:
                    for r in results:
                        r["percent"] = (r["size"] / total_scan_size) * 100
                needs_scan = False
            else:
                break
        elif action == "REFRESH":
            ScanCache._data.pop(str(target_to_scan), None)
            needs_scan = True
        elif action == "OPEN":
            path = results[idx]["path"]
            parent = path.parent if path.exists() else path
            subprocess.run(["xdg-open", str(parent)], capture_output=True)
        elif action == "DRILL_DOWN":
            item = results[idx]
            if item.get("is_smart"):
                # For smart views, show a file list of the filtered items
                top_selector = TopFilesSelector(f"Smart View: {item['name']}", item["smart_items"])
                selected_idxs = top_selector.run()
                if selected_idxs:
                    confirm_msg = f"Are you sure you want to delete {len(selected_idxs)} items?"
                    confirm = ConfirmSelector(confirm_msg)
                    if confirm.run():
                        for s_idx in selected_idxs:
                            p = item["smart_items"][s_idx]["path"]
                            if safe_remove(p, use_trash=True)[0]:
                                ScanCache.clear()
                        needs_scan = True
            elif item["path"].is_dir():
                # Safety: Avoid entering / as it's too heavy and requires sudo for full scan
                if str(item["path"]) == "/":
                    continue

                state_stack.append(
                    {
                        "target": current_target,
                        "results": results,
                        "data": data,
                        "total_size": total_scan_size,
                    }
                )
                current_target = item["path"]
                needs_scan = True
            elif item["path"].is_file():
                p = item["path"]
                archive_exts = {
                    ".zip",
                    ".tar",
                    ".gz",
                    ".xz",
                    ".bz2",
                    ".7z",
                    ".rar",
                    ".deb",
                    ".rpm",
                    ".apk",
                }
                is_archive = p.suffix.lower() in archive_exts
                is_exec = os.access(p, os.X_OK)

                if is_archive or is_exec:
                    # Open parent directory instead for safety
                    subprocess.run(["xdg-open", str(p.parent)], capture_output=True)
                else:
                    subprocess.run(["xdg-open", str(p)], capture_output=True)
        elif action == "DELETE_BATCH":
            selected_idxs = idx  # action was DELETE_BATCH, idx contains the list
            total_selected_size = sum(results[i]["size"] for i in selected_idxs)
            confirm_msg = (
                f"Delete {len(selected_idxs)} items ({bytes_to_human(total_selected_size)})?"
            )
            confirm = ConfirmSelector(confirm_msg)
            if confirm.run():
                for s_idx in selected_idxs:
                    p = results[s_idx]["path"]
                    if safe_remove(p, use_trash=True)[0]:
                        ScanCache.clear()
                needs_scan = True
        elif action == "OPEN_BATCH":
            selected_idxs = idx
            for s_idx in selected_idxs:
                p = results[s_idx]["path"]
                parent = p.parent if p.exists() else p
                subprocess.run(["xdg-open", str(parent)], capture_output=True)
