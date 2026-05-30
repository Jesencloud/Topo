import os
import shutil
import subprocess
import sys
import termios
import time
import tty
from pathlib import Path
from typing import Any

from ..core import system
from ..core.analyze import ScanCache
from ..core.constants import BOLD, GRAY, GREEN, MAGENTA, RED, RESET, YELLOW
from ..core.file_ops import bytes_to_human, parse_size_to_bytes, safe_remove
from ..ui.navigator import Navigator, UninstallSelector


class UninstallManager:
    # Tokens too short or generic to safely substring-match against folder names.
    # Matching these loosely would flag unrelated directories for deletion
    # (e.g. "desktop" from "org.telegram.desktop", or "data"/"app").
    _GENERIC_TOKENS = frozenset(
        {
            "app", "apps", "data", "core", "bin", "cache", "config", "share",
            "gui", "lib", "tmp", "temp", "default", "common", "main", "client",
            "desktop", "system", "settings", "local", "user", "code", "go", "id",
        }
    )

    def __init__(self):
        self.apps: list[dict[str, Any]] = []

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

    def _parse_size_to_bytes(self, size_str: str) -> int:
        return parse_size_to_bytes(size_str)

    def _get_app_localized_name(self, desktop_file: Path, name: str) -> str:
        """Tries to find Name[zh_CN] or Name in .desktop file."""
        english_name = ""
        try:
            with open(desktop_file, errors="ignore") as f:
                for line in f:
                    if line.startswith("Name[zh_CN]="):
                        return line.split("=")[1].strip()
                    if line.startswith("Name=") and not english_name:
                        english_name = line.split("=")[1].strip()
        except OSError:
            pass
        return english_name or name

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
        except OSError:
            pass
        return list(keywords)

    def run_full_scan(self) -> list[dict[str, Any]]:
        """Scans for user-facing applications (DNF and Flatpak)."""
        apps = []
        os_id = system.get_os_id()

        # 1. Pre-scan: Identify RPMs that provide desktop files (User Apps)
        user_app_packages = set()
        if os_id in ("fedora", "rhel", "centos") and shutil.which("rpm"):
            try:
                # Find all .desktop files in standard system paths
                desktop_dirs = [
                    "/usr/share/applications",
                    str(Path.home() / ".local/share/applications"),
                ]
                desktop_files = []
                for d in desktop_dirs:
                    p = Path(d)
                    if p.exists():
                        desktop_files.extend([str(f) for f in p.glob("*.desktop")])

                if desktop_files:
                    # Query RPM to see which package owns these desktop files
                    # We process in batches to avoid 'argument list too long'
                    batch_size = 500
                    for i in range(0, len(desktop_files), batch_size):
                        batch = desktop_files[i : i + batch_size]
                        res = subprocess.run(
                            ["rpm", "-qf", "--queryformat", "%{NAME}\n"] + batch,
                            capture_output=True,
                            text=True,
                        )
                        if res.stdout:
                            for line in res.stdout.splitlines():
                                if not line.startswith(
                                    "file "
                                ):  # Filter out 'file X is not owned by any package'
                                    user_app_packages.add(line.strip())
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

        # 2. DNF (RPM) Scan - Filtered by user_app_packages
        if os_id in ("fedora", "rhel", "centos") and shutil.which("rpm"):
            try:
                # Get all installed packages with their size and install time
                res = subprocess.run(
                    ["rpm", "-qa", "--queryformat", "%{NAME}\t%{SIZE}\t%{INSTALLTIME}\n"],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    for line in res.stdout.splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 3:
                            app_id, size_bytes, install_time = (
                                parts[0],
                                int(parts[1]),
                                int(parts[2]),
                            )

                            # SMART FILTER: Only include if it's a known user app or very large (> 100MB)
                            if app_id in user_app_packages or size_bytes > 100 * 1024 * 1024:
                                apps.append(
                                    {
                                        "id": app_id,
                                        "name": app_id,
                                        "size_bytes": size_bytes,
                                        "size_str": bytes_to_human(size_bytes),
                                        "type": "DNF",
                                        "install_time": install_time,
                                    }
                                )
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

        # 3. Flatpak Scan
        if shutil.which("flatpak"):
            try:
                res = subprocess.run(
                    ["flatpak", "list", "--app", "--columns=name,application,size,installation"],
                    capture_output=True,
                    text=True,
                )
                if res.returncode == 0:
                    for line in res.stdout.splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 3:
                            app_name, app_id, size_str = parts[0], parts[1], parts[2]

                            # Estimate install time from flatpak directory
                            install_time = 0
                            try:
                                # Standard flatpak paths
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

                            size_bytes = self._parse_size_to_bytes(size_str)
                            apps.append(
                                {
                                    "id": app_id,
                                    "name": app_name,
                                    "size_bytes": size_bytes,
                                    "size_str": size_str,
                                    "type": "Flatpak",
                                    "install_time": install_time,
                                }
                            )
            except (OSError, subprocess.SubprocessError):
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
                            if self._name_matches(entry_lower, t):
                                p = Path(entry.path)
                                if str(p) not in seen:
                                    paths.append(p)
                                    seen.add(str(p))
            except OSError:
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
                                self._name_matches(entry_lower, app_name.lower())
                                and str(entry.path) not in seen
                                and home_path in Path(entry.path).parents
                            ):
                                paths.append(Path(entry.path))
                                seen.add(str(entry.path))
            except OSError:
                pass

        return paths

    def execute_uninstall(self, app: dict[str, Any], paths: list[Path]):
        """Terminates app and removes all files."""
        # 1. Kill processes
        all_process_names = [app["id"], app["name"].lower()]
        if app["type"] == "Flatpak":
            import contextlib

            with contextlib.suppress(OSError, subprocess.SubprocessError):
                subprocess.run(["flatpak", "kill", app["id"]], capture_output=True)

        for proc in all_process_names:
            try:
                res = subprocess.run(["pgrep", "-x", proc], capture_output=True)
                if res.returncode == 0:
                    subprocess.run(["pkill", "-9", "-x", proc], capture_output=True)
                    time.sleep(0.5)
            except (OSError, subprocess.SubprocessError):
                pass

        # 2. Binary uninstall
        if app["type"] == "Flatpak":
            system.run_command(["flatpak", "uninstall", "-y", app["id"]], capture=True)
        else:
            system.run_command(["dnf", "remove", "-y", app["id"]], use_sudo=True, capture=True)

        # 3. Path removal
        removed_details = []
        for p in paths:
            success, _ = safe_remove(p, use_trash=False)
            try:
                removed_details.append((success, str(p.relative_to(Path.home()))))
            except ValueError:
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

        # --- PREVIEW LOOP ---
        # Residue discovery and process checks touch the filesystem / spawn pgrep,
        # so compute them once up front; the redraw loop below only formats them.
        selected_apps = [apps[i] for i in selected_indices]
        all_targets = []
        total_estimated_size = 0
        for app in selected_apps:
            is_running = False
            for proc in (app["id"], app["name"].lower()):
                try:
                    if subprocess.run(["pgrep", "-x", proc], capture_output=True).returncode == 0:
                        is_running = True
                        break
                except (OSError, subprocess.SubprocessError):
                    pass
            app_paths = manager.find_residue_paths(app["id"], app["name"], app["type"])
            all_targets.append((app, app_paths, is_running))
            total_estimated_size += app["size_bytes"]

        with Navigator.raw_mode() as fd:
            preview_done = False
            while not preview_done:
                buf = ["\033[H"]  # Go home
                # Use \033[K on every line, including the very first line and spacers
                buf.append(f"\033[1;35m➔\033[0m {BOLD}Uninstallation Preview{RESET}\033[K\n")
                buf.append("-" * 70 + "\033[K\n")

                for app, app_paths, is_running in all_targets:
                    running_tag = " \033[1;33m[Running]\033[0m" if is_running else ""
                    buf.append(
                        f"  \033[1;32m✓\033[0m {BOLD}{app['name']}{RESET}{running_tag}\033[K\n"
                    )
                    for p in app_paths:
                        try:
                            rel_p = f"~/{p.relative_to(Path.home())}"
                            buf.append(f"    \033[1;34m✓\033[0m {GRAY}{rel_p}{RESET}\033[K\n")
                        except ValueError:
                            buf.append(f"    \033[1;34m✓\033[0m {GRAY}{p}{RESET}\033[K\n")

                buf.append("-" * 70 + "\033[K\n")
                buf.append("\033[K\n")  # Explicit cleared spacer line
                app_text = "application" if len(selected_apps) == 1 else "applications"
                size_display = bytes_to_human(total_estimated_size)
                prompt = (
                    f" {MAGENTA}→{RESET} Remove {len(selected_apps)} {app_text}, {size_display} "
                    f" {GREEN}Enter{RESET} confirm, {GRAY}ESC{RESET} cancel: "
                )
                buf.append(prompt + "\033[K")
                buf.append("\033[J")  # Clear remaining old lines below

                sys.stdout.write("".join(buf))
                sys.stdout.flush()

                # Capture key using standardized navigator with persistent raw mode
                ch = Navigator.get_key(fd)

                if ch in Navigator.ENTER:
                    # Temporary exit raw mode for sudo prompt
                    termios.tcsetattr(fd, termios.TCSADRAIN, termios.tcgetattr(sys.stdin))
                    try:
                        # Ensure sudo session (require password)
                        print(f"\n {GRAY}🔒 Authorizing removal (Ctrl+C to cancel)...{RESET}")

                        if not system.ensure_sudo_session():
                            if system.SUDO_CANCELLED:
                                print(f"\n {YELLOW}⚠️  Uninstall cancelled by user.{RESET}")
                            else:
                                print(
                                    f"\n {RED}✗{RESET} Authorization failed. Uninstall cancelled."
                                )
                            if not Navigator.wait_for_return():
                                preview_done = True
                                break
                            continue

                        # --- EXECUTION ---
                        print(f"\n\n {GRAY}🚀 Processing...{RESET}")
                        removed_names = []
                        total_freed_all = 0

                        for app, paths, _ in all_targets:
                            manager.execute_uninstall(app, paths)
                            removed_names.append(app["name"])
                            total_freed_all += app["size_bytes"]

                        # Final Summary
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
                            return  # Exit uninstall completely
                        preview_done = True
                    finally:
                        # Re-enter cbreak mode if we are continuing the loop
                        if not preview_done:
                            tty.setcbreak(fd)
                elif ch == Navigator.ESC and len(ch) == 1:
                    # Explicit ESC: Cancel and go back to list
                    preview_done = True
                else:
                    # MOUSE_EVENT, Arrows, or other keys: Stay on preview screen
                    continue
