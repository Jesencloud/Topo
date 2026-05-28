import os
import shutil
import subprocess
import time
import sys
import select
from pathlib import Path
from typing import List, Dict, Any
from ..core.system import run_command, get_os_id
from ..core.file_ops import get_size, safe_remove, bytes_to_human
from ..core.constants import GRAY, RESET, BOLD, CYAN, MAGENTA, YELLOW, GREEN, RED

class UninstallManager:
    def __init__(self):
        self.apps: List[Dict[str, Any]] = []

    def _parse_size_to_bytes(self, size_str: str) -> int:
        if not size_str or size_str == "N/A": return 0
        try:
            val_str = ''.join(c for c in size_str if c.isdigit() or c == '.')
            val = float(val_str)
            # Use Base-10 (1000) for consistency with bytes_to_human
            if "GB" in size_str: val *= 1000**3
            elif "MB" in size_str: val *= 1000**2
            elif "KB" in size_str: val *= 1000
            return int(val)
        except:
            return 0

    def _get_localized_name(self, desktop_file: Path) -> str:
        """Parses a .desktop file to find the best localized name."""
        if not desktop_file.exists(): return None
        
        lang = os.environ.get('LANG', 'en').split('.')[0]
        name = None
        localized_name = None
        
        try:
            with open(desktop_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.startswith('Name='):
                        name = line.split('=', 1)[1].strip()
                    elif line.startswith(f'Name[{lang}]='):
                        localized_name = line.split('=', 1)[1].strip()
                    if localized_name: return localized_name
        except: pass
        return localized_name or name

    def _extract_keywords_from_desktop(self, desktop_file: Path) -> List[str]:
        """Extracts search keywords (binary name, icon name) from a .desktop file."""
        keywords = set()
        if not desktop_file or not desktop_file.exists(): return []
        
        try:
            with open(desktop_file, 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    if line.startswith('Exec='):
                        # Extract basename only (e.g. /usr/bin/wechat -> wechat)
                        exec_val = line.split('=', 1)[1].strip().split()[0]
                        bin_name = Path(exec_val).name.lower()
                        if bin_name: keywords.add(bin_name)
                    elif line.startswith('Icon='):
                        # Extract basename only for icon too
                        icon_val = line.split('=', 1)[1].strip().lower()
                        icon_name = Path(icon_val).name.split('.')[0] # Remove .png/.svg
                        if icon_name: keywords.add(icon_name)
        except: pass
        return list(keywords)

    def scan_flatpaks(self):
        if not shutil.which("flatpak"): return
        try:
            res = subprocess.run(["flatpak", "list", "--columns=name,application,size"], 
                                capture_output=True, text=True)
            if res.returncode == 0:
                for line in res.stdout.strip().split('\n'):
                    parts = line.split('\t')
                    if len(parts) >= 3:
                        name, app_id, size_str = parts[0], parts[1], parts[2]
                        
                        id_lower = app_id.lower()
                        if any(k in id_lower for k in [".platform", ".locale", ".extension", ".sdk", ".baseapp", ".vaapi"]):
                            continue
                        if ".gl." in id_lower or ".cl." in id_lower or "codecs-extra" in id_lower:
                            continue
                        
                        install_path = Path(f"/var/lib/flatpak/app/{app_id}")
                        if not install_path.exists():
                            install_path = Path.home() / f".local/share/flatpak/app/{app_id}"
                        
                        # Extract keywords for process killing and deep search
                        keywords = []
                        desktop_paths = [
                            Path(f"/var/lib/flatpak/exports/share/applications/{app_id}.desktop"),
                            Path.home() / f".local/share/flatpak/exports/share/applications/{app_id}.desktop"
                        ]
                        for dp in desktop_paths:
                            if dp.exists():
                                keywords = self._extract_keywords_from_desktop(dp)
                                break

                        self.apps.append({
                            "name": app_id, 
                            "id": app_id,
                            "type": "Flatpak",
                            "size_str": size_str,
                            "size_bytes": self._parse_size_to_bytes(size_str),
                            "install_time": os.path.getctime(install_path) if install_path.exists() else 0,
                            "keywords": keywords,
                            "data_paths": self._find_user_data(app_id, extra_keywords=keywords)
                        })
        except: pass

    def scan_snaps(self):
        if not shutil.which("snap"): return
        try:
            res = subprocess.run(["snap", "list"], capture_output=True, text=True)
            if res.returncode == 0:
                lines = res.stdout.strip().split('\n')[1:]
                for line in lines:
                    parts = line.split()
                    if parts:
                        name = parts[0]
                        keywords = self._extract_keywords_from_desktop(Path(f"/var/lib/snapd/desktop/applications/{name}_{name}.desktop"))
                        
                        self.apps.append({
                            "name": name,
                            "id": name,
                            "type": "Snap",
                            "size_str": "N/A",
                            "size_bytes": 0,
                            "install_time": os.path.getctime(f"/snap/{name}") if os.path.exists(f"/snap/{name}") else 0,
                            "keywords": keywords,
                            "data_paths": self._find_user_data(name, extra_keywords=keywords)
                        })
        except: pass

    def scan_native(self):
        os_id = get_os_id()
        desktop_globs = [
            "/usr/share/applications/*.desktop",
            str(Path.home() / ".local/share/applications/*.desktop")
        ]
        
        if os_id in ("fedora", "rhel"):
            try:
                import glob as pyglob
                all_desktops = []
                for pattern in desktop_globs:
                    all_desktops.extend(pyglob.glob(pattern))
                
                if not all_desktops: return

                batch_size = 100
                for i in range(0, len(all_desktops), batch_size):
                    batch = all_desktops[i:i + batch_size]
                    cmd = ["rpm", "-qf"] + batch + ["--qf", "%{NAME}\\t%{INSTALLTIME}\\t%{SIZE}\\n"]
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    
                    if res.stdout:
                        lines = res.stdout.strip().split('\n')
                        for idx, line in enumerate(lines):
                            if not line or "\t" not in line: continue
                            
                            parts = line.split('\t')
                            pkg_name = parts[0]
                            itime = float(parts[1]) if len(parts) > 1 else 0
                            size_bytes = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
                            
                            lower_pkg = pkg_name.lower()
                            if any(k in lower_pkg for k in ["-handler", "url-handler", "mime-handler"]):
                                continue

                            if not any(a['id'] == pkg_name for a in self.apps):
                                desktop_path = Path(batch[idx])
                                keywords = self._extract_keywords_from_desktop(desktop_path)
                                
                                self.apps.append({
                                    "name": pkg_name, 
                                    "id": pkg_name, 
                                    "type": "DNF",
                                    "size_str": bytes_to_human(size_bytes) if size_bytes > 0 else "N/A", 
                                    "size_bytes": size_bytes,
                                    "install_time": itime,
                                    "keywords": keywords,
                                    "data_paths": self._find_user_data(pkg_name, extra_keywords=keywords)
                                })
            except Exception: pass
        elif os_id in ("ubuntu", "debian", "linuxmint"):
            pass

    def _find_user_data(self, app_name: str, extra_keywords: List[str] = None) -> List[Path]:
        paths = []
        home_path = Path.home()
        
        # 1. Generate naming variants to search
        search_terms = {app_name.lower()}
        if extra_keywords:
            for kw in extra_keywords:
                # Security: Ensure kw is just a name, not a path
                kw_clean = Path(kw).name.lower()
                search_terms.add(kw_clean)
        
        final_variants = set()
        for term in search_terms:
            final_variants.add(term)
            final_variants.add(term.replace("-", ""))
            final_variants.add(term.replace(" ", ""))
            final_variants.add(term.replace("_", ""))
        
        # 2. Define search locations (ONLY in $HOME)
        search_roots = [
            home_path / ".config",
            home_path / ".local/share",
            home_path / ".cache",
            home_path / ".local/state",
            home_path / ".var/app"
        ]
        
        seen = set()
        for root in search_roots:
            if not root.exists(): continue
            # Check for direct matches
            for variant in final_variants:
                p = root / variant
                # CRITICAL SAFETY: Path MUST be inside home
                try:
                    if p.exists() and str(p) not in seen and home_path in p.parents:
                        paths.append(p)
                        seen.add(str(p))
                except: pass
            
            # 3. Fuzzy match: scan for directories containing the app_name (Deep Scan)
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        if entry.is_dir():
                            entry_lower = entry.name.lower()
                            if app_name.lower() in entry_lower and str(entry.path) not in seen:
                                # FINAL SAFETY CHECK
                                if home_path in Path(entry.path).parents:
                                    paths.append(Path(entry.path))
                                    seen.add(str(entry.path))
            except: pass
                    
        return paths

    def run_full_scan(self):
        print("  ⏳ Scanning for installed applications...")
        self.apps = []
        self.scan_flatpaks()
        self.scan_snaps()
        self.scan_native()
        
        # --- Total Footprint Calculation ---
        # Some package managers only report the binary size.
        # We add the size of all discovered config/cache folders for total accuracy.
        print("  📊 Analyzing deep footprint (configs & caches)...", end="\r")
        for app in self.apps:
            extra_bytes = 0
            for path in app.get('data_paths', []):
                if path.exists():
                    extra_bytes += get_size(path)
            
            if extra_bytes > 0:
                app['size_bytes'] += extra_bytes
                app['size_str'] = bytes_to_human(app['size_bytes'])
        
        print("  ✅ Application discovery complete.              ")
        return self.apps

    def uninstall_app(self, app_idx: int, remove_data: bool = True):
        app = self.apps[app_idx]
        removed_details = []
        app_freed_bytes = app['size_bytes']
        
        # 1. Process Termination (Ghost App Prevention)
        keywords = app.get("keywords", [])
        all_process_names = set(keywords)
        all_process_names.add(app['id'].split('.')[-1].lower())
        all_process_names.add(app['name'].lower())
        
        if app['type'] == "Flatpak":
            try:
                subprocess.run(["flatpak", "kill", app['id']], capture_output=True)
            except: pass

        for proc in all_process_names:
            if not proc: continue
            try:
                res = subprocess.run(["pgrep", "-x", proc], capture_output=True)
                if res.returncode == 0:
                    removed_details.append((False, f"Stopping active process: {proc}"))
                    subprocess.run(["pkill", "-9", "-x", proc], capture_output=True)
                    time.sleep(0.5)
            except: pass

        # 2. System Removal
        success = False
        if app['type'] == "Flatpak":
            res = run_command(["flatpak", "uninstall", "-y", app['id']], capture=False)
            success = (res.returncode == 0)
            if success:
                removed_details.append((True, f"Flatpak: {app['id']}"))
        elif app['type'] == "Snap":
            res = run_command(["snap", "remove", app['id']], use_sudo=True, capture=False)
            success = (res.returncode == 0)
            if success:
                removed_details.append((True, f"Snap: {app['id']}"))
        elif app['type'] in ("DNF", "APT"):
            if app['type'] == "DNF":
                res = run_command(["dnf", "remove", "-y", app['id']], use_sudo=True, capture=False)
                if res.returncode == 0:
                    removed_details.append((True, f"Package: {app['id']}"))
                    run_command(["dnf", "autoremove", "-y"], use_sudo=True, capture=True)
            else:
                res = run_command(["apt-get", "purge", "-y", app['id']], use_sudo=True, capture=False)
                if res.returncode == 0:
                    removed_details.append((True, f"Package: {app['id']} (purged)"))
                    run_command(["apt-get", "autoremove", "-y"], use_sudo=True, capture=True)
            success = (res.returncode == 0)

        # 3. Data Removal (ONLY IN HOME)
        if success and remove_data:
            for p in app['data_paths']:
                if p.exists():
                    s = get_size(p)
                    # We ensure p is in home during discovery, but safe_remove also checks
                    if safe_remove(p, use_trash=True)[0]:
                        app_freed_bytes += s
                        try:
                            removed_details.append((True, str(p.relative_to(Path.home()))))
                        except:
                            removed_details.append((True, str(p)))
        
        return success, app_freed_bytes, removed_details

from ..ui.navigator import UninstallSelector, Navigator
from ..core.analyze import ScanCache

def run_uninstall():
    manager = UninstallManager()
    
    while True:
        apps = manager.run_full_scan()
        
        if not apps:
            print("\n   \033[1;31mNo applications found to uninstall.\033[0m")
            Navigator.wait_for_return()
            return

        selector = UninstallSelector("Select Application to Uninstall", apps)
        selected_indices = selector.run()
        
        if not selected_indices:
            return # Back to main menu

        # --- MOLE STYLE PREVIEW ---
        os.system('clear')
        total_estimated_size = 0
        from ..core.file_ops import bytes_to_human
        
        print(f"\n \033[1;35m☉ Reviewing uninstallation plan:\033[0m\n")
        
        for idx in selected_indices:
            app = apps[idx]
            # Check if running for the [Running] tag
            is_running = False
            # Check keywords and some common process names
            procs_to_check = app.get('keywords', []) + [app['id'].split('.')[-1].lower(), app['name'].lower()]
            for proc in procs_to_check:
                if not proc: continue
                try:
                    res = subprocess.run(["pgrep", "-x", proc], capture_output=True)
                    if res.returncode == 0:
                        is_running = True; break
                except: pass
            
            running_tag = f" \033[1;33m[Running]\033[0m" if is_running else ""
            print(f"  \033[1;32m✓\033[0m {BOLD}{app['name']}{RESET}{running_tag}")
            total_estimated_size += app['size_bytes']
            
            # Show paths with Mole-style icons
            for p in app['data_paths']:
                if p.exists():
                    try:
                        rel_p = f"~/{p.relative_to(Path.home())}"
                        print(f"    \033[1;34m✓\033[0m {GRAY}{rel_p}{RESET}")
                    except:
                        print(f"    \033[1;34m✓\033[0m {GRAY}{p}{RESET}")
        
        # --- MOLE STYLE CONFIRMATION LINE ---
        app_text = "app" if len(selected_indices) == 1 else "apps"
        size_display = bytes_to_human(total_estimated_size)
        
        print(f"\n \033[1;35m→\033[0m Remove {len(selected_indices)} {app_text}, {size_display}  \033[1;32mEnter\033[0m confirm, \033[1;90mESC\033[0m cancel: ", end="", flush=True)

        # Capture single key
        import tty, termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
            # Handle ESC (\x1b)
            if ch == '\x1b':
                # Check if it's a sequence or just ESC
                # Non-blocking read to see if there's more
                import select
                if select.select([sys.stdin], [], [], 0)[0]:
                    ch = 'ESC_SEQ' # It's an arrow key or something else
                else:
                    ch = 'ESC'
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        
        if ch not in ('\r', '\n', 'y', 'Y'):
            print(f"\n\n {GRAY}Uninstallation cancelled.{RESET}")
            time.sleep(1)
            continue

        # --- SUDO AUTHORIZATION ---
        print(f"\n\n {GRAY}🔒 Authorizing system-level tasks (Ctrl+C to cancel)...{RESET}")
        from ..core.system import ensure_sudo_session
        if not ensure_sudo_session():
            print(f" \033[1;33m⚠️  Uninstallation cancelled by user.\033[0m")
            time.sleep(1)
            continue

        # --- EXECUTION ---
        total_freed_all = 0
        removed_names = []
        
        os.system('clear')
        print(f"\033[1;35m🚀 Executing uninstallation...\033[0m\n")

        for idx in selected_indices:
            app = apps[idx]
            print(f"\033[1;35m☉\033[0m {BOLD}{app['name']}{RESET}")
            
            success, freed_bytes, details = manager.uninstall_app(idx)
            if success:
                total_freed_all += freed_bytes
                removed_names.append(app['name'])
                for is_ok, path in details:
                    # Using icons and colors from the screenshot
                    if is_ok:
                        print(f"  \033[0;32m✓\033[0m {path}")
                    else:
                        # Non-file actions (like stopping processes)
                        print(f"  \033[1;35m☉\033[0m {path}")
                print(f"\033[0;32m[✓]\033[0m {app['name']} removed.\n")
            else:
                print(f"\033[1;31m[✗]\033[0m {app['name']} uninstallation failed.\n")

        # Final Summary (Pixel-perfect matching of the screenshot)
        if total_freed_all > 0: ScanCache.clear()
        print("=" * 70)
        print("\033[1;34mUninstall complete\033[0m")
        names_str = ", ".join(removed_names)
        print(f"Removed {len(removed_names)} app(s), freed \033[1;32m{bytes_to_human(total_freed_all)}\033[0m: {names_str}")
        print("=" * 70)

        # Standardized return/exit prompt
        if not Navigator.wait_for_return():
            break

