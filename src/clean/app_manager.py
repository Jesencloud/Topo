import os
import select
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..core.analyze import ScanCache
from ..core.constants import BOLD, GRAY, GREEN, MAGENTA, RESET
from ..core.file_ops import bytes_to_human, safe_remove
from ..core.system import get_os_id, run_command
from ..ui.navigator import Navigator, UninstallSelector


class UninstallManager:
    def __init__(self):
        self.apps: list[dict[str, Any]] = []

    def _parse_size_to_bytes(self, size_str: str) -> int:
        if not size_str or size_str == "N/A":
            return 0
        try:
            val_str = "".join(c for c in size_str if c.isdigit() or c == ".")
            val = float(val_str)
            unit = size_str.upper()
            if "G" in unit:
                val *= 1024**3
            elif "M" in unit:
                val *= 1024**2
            elif "K" in unit:
                val *= 1024
            return int(val)
        except Exception:
            return 0

    def _get_app_localized_name(self, desktop_file: Path, name: str) -> str:
        """Tries to find Name[zh_CN] or Name in .desktop file."""
        localized_name = ""
        try:
            with open(desktop_file, errors="ignore") as f:
                for line in f:
                    if line.startswith("Name[zh_CN]="):
                        return line.split("=")[1].strip()
                    if line.startswith("Name="):
                        localized_name = line.split("=")[1].strip()
                    if localized_name:
                        return localized_name
        except Exception:
            pass
        return localized_name or name

    def _get_app_keywords(self, desktop_file: Path) -> list[str]:
        """Extracts potential folder name keywords from Exec and Icon fields."""
        keywords = set()
        try:
            with open(desktop_file, errors="ignore") as f:
                for line in f:
                    if line.startswith("Exec="):
                        cmd = line.split("=")[1].split()[0].split("/")[-1].strip("'\"")
                        keywords.add(cmd.lower())
                    if line.startswith("Icon="):
                        icon_name = line.split("=")[1].strip().lower()
                        if icon_name:
                            keywords.add(icon_name)
        except Exception:
            pass
        return list(keywords)

    def run_full_scan(self) -> list[dict[str, Any]]:
        """Scans for both DNF (RPM) and Flatpak applications."""
        apps = []
        os_id = get_os_id()

        # 1. DNF (RPM) Scan
        if os_id in ("fedora", "rhel", "centos") and shutil.which("rpm"):
            try:
                # Get all installed packages with their size
                res = subprocess.run(
                    ["rpm", "-qa", "--queryformat", "%{NAME}\t%{SIZE}\n"],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    for line in res.stdout.splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 2:
                            app_id, size_bytes = parts[0], int(parts[1])
                            apps.append(
                                {
                                    "id": app_id,
                                    "name": app_id,
                                    "size_bytes": size_bytes,
                                    "size_str": bytes_to_human(size_bytes),
                                    "type": "DNF",
                                    "install_time": 0,
                                }
                            )
            except Exception:
                pass

        # 2. Flatpak Scan
        if shutil.which("flatpak"):
            try:
                res = subprocess.run(
                    ["flatpak", "list", "--app", "--columns=name,application,size"],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    for line in res.stdout.splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 3:
                            app_name, app_id, size_str = parts[0], parts[1], parts[2]
                            id_lower = app_id.lower()
                            if "org.freedesktop" in id_lower or "org.gnome.platform" in id_lower:
                                continue

                            size_bytes = self._parse_size_to_bytes(size_str)
                            apps.append(
                                {
                                    "id": app_id,
                                    "name": app_name,
                                    "size_bytes": size_bytes,
                                    "size_str": size_str,
                                    "type": "Flatpak",
                                    "install_time": 0,
                                }
                            )
            except Exception:
                pass

        self.apps = sorted(apps, key=lambda x: x["size_bytes"], reverse=True)
        return self.apps

    def find_residue_paths(self, app_id: str, app_name: str, app_type: str) -> list[Path]:
        """Finds all data/config/cache paths associated with an app."""
        paths = []
        home_path = Path.home()
        seen = set()

        # 1. Standard XDG paths
        search_roots = [
            home_path / ".config",
            home_path / ".local/share",
            home_path / ".cache",
            home_path / ".var/app",  # Flatpak
        ]

        # 2. Common variants of the name
        targets = {app_id.lower(), app_name.lower()}
        if "." in app_id:
            targets.add(app_id.split(".")[-1].lower())

        # 3. .desktop file keywords
        desktop_paths = [
            Path(f"/usr/share/applications/{app_id}.desktop"),
            home_path / f".local/share/applications/{app_id}.desktop",
        ]
        for dp in desktop_paths:
            if dp.exists():
                targets.update(self._get_app_keywords(dp))

        # 4. Search
        for root in search_roots:
            if not root.exists():
                continue
            try:
                with os.scandir(root) as it:
                    for entry in it:
                        entry_lower = entry.name.lower()
                        for t in targets:
                            if t in entry_lower:
                                p = Path(entry.path)
                                if str(p) not in seen:
                                    paths.append(p)
                                    seen.add(str(p))
            except Exception:
                pass

        # 5. Wine prefix check (optional, if wechat/etc)
        if "wechat" in app_name.lower():
            wine_p = home_path / ".xwechat"
            if wine_p.exists() and str(wine_p) not in seen:
                paths.append(wine_p)
                seen.add(str(wine_p))

        # 6. Deep Subdirectory Search (if name is specific enough)
        if len(app_name) > 3:
            try:
                # Only scan top-level dirs in home for speed/safety
                with os.scandir(home_path) as it:
                    for entry in it:
                        if entry.is_dir():
                            entry_lower = entry.name.lower()
                            if (
                                app_name.lower() in entry_lower
                                and str(entry.path) not in seen
                                and home_path in Path(entry.path).parents
                            ):
                                paths.append(Path(entry.path))
                                seen.add(str(entry.path))
            except Exception:
                pass

        return paths

    def execute_uninstall(self, app: dict[str, Any], paths: list[Path]):
        """Terminates app and removes all files."""
        # 1. Kill processes
        all_process_names = [app["id"], app["name"].lower()]
        if app["type"] == "Flatpak":
            import contextlib

            with contextlib.suppress(Exception):
                subprocess.run(["flatpak", "kill", app["id"]], capture_output=True)

        for proc in all_process_names:
            try:
                res = subprocess.run(["pgrep", "-x", proc], capture_output=True)
                if res.returncode == 0:
                    subprocess.run(["pkill", "-9", "-x", proc], capture_output=True)
                    time.sleep(0.5)
            except Exception:
                pass

        # 2. Binary uninstall
        if app["type"] == "Flatpak":
            run_command(["flatpak", "uninstall", "-y", app["id"]], capture=True)
        else:
            run_command(["dnf", "remove", "-y", app["id"]], use_sudo=True, capture=True)

        # 3. Path removal
        removed_details = []
        for p in paths:
            success, _ = safe_remove(p, use_trash=False)
            try:
                removed_details.append((success, str(p.relative_to(Path.home()))))
            except Exception:
                removed_details.append((success, str(p)))

        return removed_details


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
            return

        # --- PREVIEW ---
        os.system("clear")
        print(f"\n \033[1;35m➔\033[0m {BOLD}Uninstallation Preview{RESET}")
        print("-" * 70)

        selected_apps = [apps[i] for i in selected_indices]
        all_targets = []
        total_estimated_size = 0

        for app in selected_apps:
            is_running = False
            # Check multiple process names
            procs_to_check = [app["id"], app["name"].lower()]
            for proc in procs_to_check:
                try:
                    res = subprocess.run(["pgrep", "-x", proc], capture_output=True)
                    if res.returncode == 0:
                        is_running = True
                        break
                except Exception:
                    pass

            running_tag = " \033[1;33m[Running]\033[0m" if is_running else ""
            print(f"  \033[1;32m✓\033[0m {BOLD}{app['name']}{RESET}{running_tag}")
            total_estimated_size += app["size_bytes"]

            # Show paths with Mole-style icons
            app_paths = manager.find_residue_paths(app["id"], app["name"], app["type"])
            all_targets.append((app, app_paths))
            for p in app_paths:
                try:
                    rel_p = f"~/{p.relative_to(Path.home())}"
                    print(f"    \033[1;34m✓\033[0m {GRAY}{rel_p}{RESET}")
                except Exception:
                    print(f"    \033[1;34m✓\033[0m {GRAY}{p}{RESET}")

        print("-" * 70)
        app_text = "application" if len(selected_apps) == 1 else "applications"
        size_display = bytes_to_human(total_estimated_size)
        prompt = (
            f"\n {MAGENTA}→{RESET} Remove {len(selected_apps)} {app_text}, {size_display} "
            f" {GREEN}Enter{RESET} confirm, {GRAY}ESC{RESET} cancel: "
        )
        print(prompt, end="", flush=True)

        # Capture single key
        import termios

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            import tty

            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
            if ch == "\x1b":  # ESC
                ch = "ESC_SEQ" if select.select([sys.stdin], [], [], 0)[0] else "ESC"
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        if ch in ("\r", "\n"):
            # --- EXECUTION ---
            print(f"\n\n {GRAY}🚀 Processing...{RESET}")
            removed_names = []
            total_freed_all = 0

            for app, paths in all_targets:
                manager.execute_uninstall(app, paths)
                removed_names.append(app["name"])
                total_freed_all += app["size_bytes"]

            # Final Summary (Pixel-perfect matching of the screenshot)
            if total_freed_all > 0:
                ScanCache.clear()
            print("=" * 70)
            print("\033[1;34mUninstall complete\033[0m")
            names_str = ", ".join(removed_names)
            msg = f"Removed {len(removed_names)} app(s), freed \033[1;32m"
            msg += f"{bytes_to_human(total_freed_all)}\033[0m: {names_str}"
            print(msg)
            print("=" * 70)

            # Standardized return/exit prompt
            if not Navigator.wait_for_return():
                break
        else:
            # ESC or other key
            continue
