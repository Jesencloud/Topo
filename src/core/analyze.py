import os
import subprocess
import json
import time
import shutil
from pathlib import Path
from typing import List, Dict, Any
from .file_ops import get_size, bytes_to_human, safe_remove
from ..ui.navigator import Navigator, AnalyzeSelector, TopFilesSelector, ConfirmSelector
from ..ui.tui import show_banner

# ANSI Colors
BLUE = "\033[1;34m"
CYAN = "\033[1;36m"
MAGENTA = "\033[1;35m"
YELLOW = "\033[1;33m"
WHITE = "\033[1;37m"
GRAY = "\033[1;90m"
RESET = "\033[0m"
BOLD = "\033[1m"

VIRTUAL_DIRS = {
    "proc", "sys", "dev", "run", "snap", "flatpak", 
    "mnt", "media", "lost+found", 
    "ollama", "cuda",
    "usr", "lib", "lib64", "boot", "sbin", "bin"
}

class ScanCache:
    _data = {}
    @classmethod
    def get(cls, path: Path): return cls._data.get(str(path))
    @classmethod
    def set(cls, path: Path, data): cls._data[str(path)] = data

def get_age_hint(path: Path) -> str:
    """Calculates and formats the age of a file or directory."""
    try:
        mtime = path.stat().st_mtime
        days = (time.time() - mtime) / 86400
        if days < 90: return ""
        if days >= 365: return f">{int(days/365)}y"
        if days >= 30: return f">{int(days/30)}mo"
        return f">{int(days)}d"
    except: return ""

def get_old_items_info(dir_path: Path, days_threshold: int = 90) -> List[Dict[str, Any]]:
    """Returns a list of items in a directory older than X days."""
    old_items = []
    threshold_time = time.time() - (days_threshold * 86400)
    try:
        for item in dir_path.iterdir():
            if item.name.startswith('.'): continue
            mtime = item.stat().st_mtime
            if mtime < threshold_time:
                size = get_size(item)
                old_items.append({
                    "name": item.name,
                    "path": item,
                    "size": size,
                    "age_hint": get_age_hint(item),
                    "icon": "📁" if item.is_dir() else "📄"
                })
    except: pass
    return old_items

def get_rust_scan_data(path: Path) -> Dict[str, Any]:
    cached = ScanCache.get(path)
    if cached: return cached
    
    # 1. Detect architecture for binary selection
    import platform
    arch = platform.machine().lower()
    
    # Try architecture-specific binary first, then fallback
    bin_dir = Path(__file__).parent / "bin"
    if "x86_64" in arch or "amd64" in arch:
        bin_path = bin_dir / "topo-core-x86_64"
    elif "aarch64" in arch or "arm64" in arch:
        bin_path = bin_dir / "topo-core-aarch64"
    else:
        bin_path = bin_dir / "topo-core"

    # Final fallback if arch-specific not found
    if not bin_path.exists():
        bin_path = bin_dir / "topo-core"
        
    if not bin_path.exists(): return None
    
    try:
        res = subprocess.run([str(bin_path), str(path)], capture_output=True, text=True, timeout=30)
        if res.returncode == 0:
            data = json.loads(res.stdout)
            ScanCache.set(path, data)
            return data
    except: pass
    return None

def get_dir_size_recursive(path: Path) -> int:
    """Fast size calculation using Rust core, falls back to du."""
    if str(path) == "/":
        try: return shutil.disk_usage("/").used
        except: pass
    data = get_rust_scan_data(path)
    if data: return data.get("total_size_bytes", 0)
    try:
        res = subprocess.run(["du", "-sbx", str(path)], capture_output=True, text=True, timeout=5)
        if res.returncode == 0: return int(res.stdout.split()[0])
    except: pass
    return 0

def run_deep_analysis():
    # State Stack stores: {"target": Path, "results": [], "data": {}, "total_size": int}
    state_stack = []
    
    # Current active state
    current_target = None
    results = []
    data = None
    total_scan_size = 0
    needs_scan = True

    while True:
        target_to_scan = current_target or Path.home()
        view_title = "Analyze Disk" if current_target is None else f"Exploring: {current_target}"
        
        if needs_scan:
            print(f"   🚀 Rust Engine: Intelligence Scan on {target_to_scan.name if current_target else 'Home'}...", end="\r")
            data = get_rust_scan_data(target_to_scan)
            if not data:
                print(f"\n   ❌ Engine scan failed."); time.sleep(1.5)
                if state_stack:
                    prev = state_stack.pop()
                    current_target, results, data, total_scan_size = prev['target'], prev['results'], prev['data'], prev['total_size']
                    needs_scan = False; continue
                else: break

            total_scan_size = data.get("total_size_bytes", 0)
            results = []
            
            if current_target is None:
                # Root View: Standard Categories
                total_used = shutil.disk_usage("/").used
                targets = [
                    {"name": "Home", "path": Path.home(), "color": CYAN},
                    {"name": "Applications", "path": Path("/usr/share/applications"), "color": MAGENTA},
                    {"name": "System", "path": Path("/usr"), "color": BLUE},
                    {"name": "Root (/)", "path": Path("/"), "color": WHITE},
                ]
                for t in targets:
                    if t['path'].exists():
                        if t['path'] == Path.home(): size = total_scan_size
                        elif str(t['path']) == "/": size = total_used
                        else:
                            t_data = get_rust_scan_data(t['path'])
                            size = t_data.get("total_size_bytes", 0) if t_data else 0
                        results.append({
                            "name": t['name'], "path": t['path'], "size": size, 
                            "percent": (size / total_used) * 100, "color": t['color'], 
                            "icon": "📁", "age_hint": get_age_hint(t['path'])
                        })
                
                # --- LINUX INSIGHTS: Detect hidden space killers ---
                print(f"   🔍 Analyzing Linux Insights...", end="\r")
                home = Path.home()
                insights = [
                    {"name": "Old Downloads (90d+)", "path": home / "Downloads", "is_smart": True},
                    {"name": "Docker Data", "path": home / ".docker"},
                    {"name": "Docker System", "path": Path("/var/lib/docker")},
                    {"name": "System Logs", "path": Path("/var/log")},
                    {"name": "Apt Cache", "path": Path("/var/cache/apt/archives")},
                    {"name": "Pacman Cache", "path": Path("/var/cache/pacman/pkg")},
                    {"name": "Dnf Cache", "path": Path("/var/cache/dnf")},
                    {"name": "User Trash", "path": home / ".local/share/Trash"},
                    {"name": "Pip Cache", "path": home / ".cache/pip"},
                    {"name": "Npm Cache", "path": home / ".npm"},
                    {"name": "Cargo Cache", "path": home / ".cargo"},
                    {"name": "Snap Data", "path": home / "snap"},
                    {"name": "Flatpak Data", "path": home / ".local/share/flatpak"},
                    {"name": "Ollama Models", "path": home / ".ollama" / "models"},
                ]
                
                for ins in insights:
                    p = ins['path']
                    if p.exists():
                        size = 0
                        smart_items = []
                        
                        if ins.get('is_smart'):
                            # For smart views, we pre-calculate filtered items
                            smart_items = get_old_items_info(p)
                            size = sum(item['size'] for item in smart_items)
                        else:
                            ins_data = get_rust_scan_data(p)
                            size = ins_data.get("total_size_bytes", 0) if ins_data else 0
                        
                        if size > 10 * 1024 * 1024: # Only show if > 10MB to keep Root clean
                            results.append({
                                "name": ins['name'], "path": p, "size": size,
                                "percent": (size / total_used) * 100, "color": YELLOW, 
                                "icon": "👀", "age_hint": get_age_hint(p),
                                "is_smart": ins.get('is_smart'),
                                "smart_items": smart_items
                            })
            else:
                total_path_size = total_scan_size or 1
                subdir_map = data.get("subdirs", {})
                for name, size in subdir_map.items():
                    if name.startswith('.'): continue
                    full_path = current_target / name
                    icon = "📁" if full_path.is_dir() else "📄"
                    results.append({
                        "name": name, "path": full_path, "size": size, 
                        "percent": (size / total_path_size) * 100, "color": CYAN, 
                        "icon": icon, "age_hint": get_age_hint(full_path)
                    })
                results.sort(key=lambda x: x['size'], reverse=True)
                results = results[:50]
            needs_scan = False

        selector = AnalyzeSelector(view_title, results, show_banner=show_banner if current_target is None else None, can_select=(current_target is not None))
        action, idx = selector.run()
        
        if action == "QUIT": break
        elif action == "BACK":
            if state_stack:
                prev = state_stack.pop()
                current_target, results, data, total_scan_size = prev['target'], prev['results'], prev['data'], prev['total_size']
                # Recalculate parent percentages to reflect any deletions done in child
                if total_scan_size > 0:
                    for r in results: r['percent'] = (r['size'] / total_scan_size) * 100
                needs_scan = False
            else: break
        elif action == "REFRESH":
            ScanCache._data.pop(str(target_to_scan), None)
            needs_scan = True
        elif action == "OPEN":
            path = results[idx]['path']
            parent = path.parent if path.exists() else path
            subprocess.run(["xdg-open", str(parent)], capture_output=True)
        elif action == "OPEN_BATCH":
            # Open the containing folder for all selected items (avoid duplicates)
            parents = {results[i]['path'].parent for i in idx}
            for p in parents:
                subprocess.run(["xdg-open", str(p)], capture_output=True)
        elif action == "SWITCH_FILES":
            _render_top_files(data)
        elif action == "DELETE" or action == "DELETE_BATCH":
            # Handle deletion with parent sync
            selected_indices = [idx] if action == "DELETE" else sorted(idx, reverse=True)
            selected_items = [results[i] for i in selected_indices]
            msg = f"Delete {selected_items[0]['icon']} {selected_items[0]['name']}?" if action == "DELETE" else f"Delete {len(selected_items)} selected items?"
            
            if ConfirmSelector(msg).run():
                print(f"\n   🧹 Purging items...")
                freed_size = 0
                for item in selected_items:
                    success, m = safe_remove(item['path'], use_trash=True)
                    if success: freed_size += item['size']
                    print(f"   [{'OK' if success else '!!'}] {item['name']}: {m}")
                
                # Update current view
                for i in selected_indices: results.pop(i)
                total_scan_size = max(1, total_scan_size - freed_size)
                for r in results: r['percent'] = (r['size'] / total_scan_size) * 100
                ScanCache._data.pop(str(target_to_scan), None)
                
                # SYNC PARENT SIZE: Update the folder entry in the parent state
                if state_stack:
                    state_stack[-1]['total_size'] = max(1, state_stack[-1]['total_size'] - freed_size)
                    parent_results = state_stack[-1]['results']
                    for p_item in parent_results:
                        if p_item['path'] == target_to_scan:
                            p_item['size'] = max(0, p_item['size'] - freed_size)
                            break
                    ScanCache._data.pop(str(state_stack[-1]['target']), None)

                print(f"   ✅ Batch deletion completed. Freed {bytes_to_human(freed_size)}.")
                time.sleep(1.0)
                
                if not results and current_target is not None:
                    print("   📂 Directory empty, returning..."); time.sleep(0.6)
                    prev = state_stack.pop()
                    current_target, results, data, total_scan_size = prev['target'], prev['results'], prev['data'], prev['total_size']
                    needs_scan = False # INSTANT BACK!
            continue
        elif action == "DRILL_DOWN":
            item = results[idx]
            target = item['path']
            
            if target.is_dir():
                if item.get('is_smart'):
                    # ENTER SMART VIEW: Use pre-calculated items
                    state_stack.append({
                        "target": current_target, "results": list(results), 
                        "data": data, "total_size": total_scan_size
                    })
                    current_target = target
                    results = []
                    total_scan_size = item['size']
                    for s_item in item['smart_items']:
                        results.append({
                            **s_item,
                            "percent": (s_item['size'] / total_scan_size) * 100 if total_scan_size > 0 else 0,
                            "color": CYAN
                        })
                    results.sort(key=lambda x: x['size'], reverse=True)
                    needs_scan = False
                    continue

                if item['size'] == 0:
                    print(f"\n   📂 {item['name']} is empty."); time.sleep(1); continue
                if target.name in VIRTUAL_DIRS and str(target) != "/":
                    print(f"\n   🛡️  Protected Directory. Deep scan disabled."); time.sleep(1); continue
                
                # Push current state before entering new one
                state_stack.append({
                    "target": current_target, "results": list(results), 
                    "data": data, "total_size": total_scan_size
                })
                current_target = target
                needs_scan = True
            else:
                # If it's a file, check if it's an archive to prevent accidental extraction
                target = results[idx]['path']
                ext = target.suffix.lower()
                archives = {'.zip', '.tar', '.gz', '.7z', '.rar', '.xz', '.bz2', '.tgz', '.tbz2', '.txz', '.deb', '.rpm'}
                
                if ext in archives:
                    print(f"\n   📦 {target.name} is an archive. Auto-open disabled to prevent extraction.")
                    print(f"      Use 'F' to locate it in your file manager."); time.sleep(2.0)
                else:
                    subprocess.run(["xdg-open", str(target)], capture_output=True)

def _render_top_files(data):
    top_files = data.get("top_files", [])
    if not top_files:
        print("\n   No large files detected."); time.sleep(1.5); return
    
    # Add age hints to top files
    for item in top_files:
        item['age_hint'] = get_age_hint(Path(item['path']))

    while True:
        selector = TopFilesSelector("🏆 Top Largest Files (Interactive Deletion)", top_files)
        selected_indices = selector.run()
        if not selected_indices: break
        selected_paths = [Path(top_files[i]['path']) for i in selected_indices]
        if ConfirmSelector(f"Delete {len(selected_paths)} large files?").run():
            print("\n   🧹 Purging selected files...")
            for path in selected_paths:
                success, msg = safe_remove(path, use_trash=True)
                print(f"   [{'OK' if success else '!!'}] {path.name}: {msg}")
            for i in sorted(selected_indices, reverse=True): top_files.pop(i)
            print("\n   Done."); time.sleep(1.5)
            if not top_files: break

def _run_top_files_view(search_path="~"):
    root = Path(search_path).expanduser().resolve()
    print(f"\n🚀 Rust Engine: Scanning for top files in {root}...")
    data = get_rust_scan_data(root)
    if not data:
        print("   ❌ Rust engine failed."); time.sleep(1.5); return
    _render_top_files(data)
