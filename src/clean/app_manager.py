import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..core import system
from ..core.analyze import ScanCache
from ..core.constants import BOLD, GRAY, GREEN, MAGENTA, RED, RESET, YELLOW
from ..core.file_ops import (
    bytes_to_human,
    parse_size_to_bytes,
    record_deletion_audit,
    safe_remove,
)
from ..core.history import record_history_session
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
    _SYSTEM_COMPONENT_TOKENS = frozenset(
        {
            "akmod",
            "cinnamon",
            "gnome-control-center",
            "gnome-session",
            "gnome-settings-daemon",
            "gnome-shell",
            "gnome-software",
            "ibus",
            "kernel",
            "kmod",
            "kwin",
            "linux-headers",
            "linux-image",
            "mesa",
            "mutter",
            "networkmanager",
            "nvidia-driver",
            "pipewire",
            "plasma",
            "pulseaudio",
            "systemd",
            "wayland",
            "wireplumber",
            "xfce",
            "xorg",
        }
    )
    _SYSTEM_COMPONENT_EXACT_IDS = frozenset(
        {
            "dconf",
            "gdm",
            "gnome-browser-connector",
            "gnome-color-manager",
            "gnome-disk-utility",
            "gnome-initial-setup",
            "gnome-logs",
            "gnome-online-accounts",
            "gnome-system-monitor",
            "gnome-terminal",
            "gvfs",
            "libreoffice-core",
            "libreoffice-xsltfilter",
            "nautilus",
            "xdg-desktop-portal",
            "xdg-desktop-portal-gnome",
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

    @staticmethod
    def _app_text(app_id: str, app_name: str) -> str:
        return f"{app_id} {app_name}".lower()

    @classmethod
    def _requires_official_only_uninstall(cls, app_id: str, app_name: str) -> bool:
        text = cls._app_text(app_id, app_name)
        return any(token in text for token in cls._OFFICIAL_ONLY_TOKENS)

    @classmethod
    def _is_system_component(cls, app_id: str, app_name: str) -> bool:
        text = cls._app_text(app_id, app_name)
        app_id_lower = app_id.lower()
        return app_id_lower in cls._SYSTEM_COMPONENT_EXACT_IDS or any(
            token in text for token in cls._SYSTEM_COMPONENT_TOKENS
        )

    def _parse_size_to_bytes(self, size_str: str) -> int:
        return parse_size_to_bytes(size_str)

    @staticmethod
    def _strip_package_arch(package_name: str) -> str:
        return package_name.split(":", 1)[0]

    @staticmethod
    def _app_record(
        app_id: str,
        name: str,
        size_bytes: int,
        size_str: str,
        app_type: str,
        install_time: int = 0,
    ) -> dict[str, Any]:
        return {
            "id": app_id,
            "name": name,
            "size_bytes": size_bytes,
            "size_str": size_str,
            "type": app_type,
            "install_time": install_time,
        }

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
        """Scans for user-facing applications across Linux package managers."""
        apps = []
        os_id = system.get_os_id()

        # 1. Pre-scan: identify native packages that provide desktop files.
        user_app_packages = set()
        try:
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
                batch_size = 500
                for i in range(0, len(desktop_files), batch_size):
                    batch = desktop_files[i : i + batch_size]
                    if os_id in ("fedora", "rhel", "centos") and shutil.which("rpm"):
                        res = system.run_command(
                            ["rpm", "-qf", "--queryformat", "%{NAME}\n"] + batch,
                            capture=True,
                            timeout=60,
                        )
                        if res.stdout:
                            for line in res.stdout.splitlines():
                                if not line.startswith(
                                    "file "
                                ):  # Filter out 'file X is not owned by any package'
                                    user_app_packages.add(line.strip())
                    elif os_id in (
                        "ubuntu",
                        "debian",
                        "linuxmint",
                        "pop",
                        "elementary",
                    ) and shutil.which("dpkg-query"):
                        res = system.run_command(["dpkg-query", "-S", *batch], capture=True)
                        for line in res.stdout.splitlines():
                            if ":" in line:
                                user_app_packages.add(
                                    self._strip_package_arch(line.split(":", 1)[0].strip())
                                )
                    elif os_id in ("arch", "manjaro", "endeavouros") and shutil.which("pacman"):
                        res = system.run_command(["pacman", "-Qo", *batch], capture=True)
                        for line in res.stdout.splitlines():
                            if " is owned by " in line:
                                owned = line.split(" is owned by ", 1)[1].split()
                                if owned:
                                    user_app_packages.add(owned[0])
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

        # 2. DNF (RPM) Scan - Filtered by user_app_packages
        if os_id in ("fedora", "rhel", "centos") and shutil.which("rpm"):
            try:
                # Get all installed packages with their size and install time
                res = system.run_command(
                    ["rpm", "-qa", "--queryformat", "%{NAME}\t%{SIZE}\t%{INSTALLTIME}\n"],
                    capture=True,
                    timeout=60,
                )
                if res.ok:
                    for line in res.stdout.splitlines():
                        parts = line.split("\t")
                        if len(parts) >= 3:
                            app_id, size_bytes, install_time = (
                                parts[0],
                                int(parts[1]),
                                int(parts[2]),
                            )

                            # SMART FILTER: Only include if it's a known user app or very large (> 100MB)
                            if self._is_system_component(app_id, app_id):
                                continue
                            if app_id in user_app_packages or size_bytes > 100 * 1024 * 1024:
                                apps.append(
                                    self._app_record(
                                        app_id,
                                        app_id,
                                        size_bytes,
                                        bytes_to_human(size_bytes),
                                        "DNF",
                                        install_time,
                                    )
                                )
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

        # 3. APT (DEB) Scan - Filtered by desktop packages or large packages
        if os_id in ("ubuntu", "debian", "linuxmint", "pop", "elementary") and shutil.which(
            "dpkg-query"
        ):
            try:
                res = system.run_command(
                    ["dpkg-query", "-W", "-f=${binary:Package}\t${Installed-Size}\n"],
                    capture=True,
                    timeout=60,
                )
                if res.ok:
                    for line in res.stdout.splitlines():
                        parts = line.split("\t")
                        if len(parts) < 2:
                            continue
                        app_id = self._strip_package_arch(parts[0].strip())
                        try:
                            size_bytes = int(parts[1]) * 1024
                        except ValueError:
                            continue
                        if self._is_system_component(app_id, app_id):
                            continue
                        if app_id in user_app_packages or size_bytes > 100 * 1024 * 1024:
                            apps.append(
                                self._app_record(
                                    app_id,
                                    app_id,
                                    size_bytes,
                                    bytes_to_human(size_bytes),
                                    "APT",
                                )
                            )
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

        # 4. Pacman Scan - Filtered by desktop packages or large packages
        if os_id in ("arch", "manjaro", "endeavouros") and shutil.which("pacman"):
            try:
                res = system.run_command(["pacman", "-Qi"], capture=True, timeout=60)
                if res.ok:
                    package: dict[str, str] = {}
                    for line in [*res.stdout.splitlines(), ""]:
                        if not line.strip():
                            app_id = package.get("Name", "").strip()
                            size_bytes = self._parse_size_to_bytes(
                                package.get("Installed Size", "")
                            )
                            if (
                                app_id
                                and not self._is_system_component(app_id, app_id)
                                and (app_id in user_app_packages or size_bytes > 100 * 1024 * 1024)
                            ):
                                apps.append(
                                    self._app_record(
                                        app_id,
                                        app_id,
                                        size_bytes,
                                        bytes_to_human(size_bytes),
                                        "Pacman",
                                    )
                                )
                            package = {}
                            continue
                        if ":" in line:
                            key, value = line.split(":", 1)
                            package[key.strip()] = value.strip()
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

        # 5. Flatpak Scan
        if shutil.which("flatpak"):
            try:
                res = system.run_command(
                    ["flatpak", "list", "--app", "--columns=name,application,size,installation"],
                    capture=True,
                    timeout=60,
                )
                if res.ok:
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
                            if self._is_system_component(app_id, app_name):
                                continue

                            size_bytes = self._parse_size_to_bytes(size_str)
                            apps.append(
                                self._app_record(
                                    app_id, app_name, size_bytes, size_str, "Flatpak", install_time
                                )
                            )
            except (OSError, subprocess.SubprocessError):
                pass

        # 6. Snap Scan
        if shutil.which("snap"):
            try:
                res = system.run_command(["snap", "list"], capture=True, timeout=60)
                if res.ok:
                    for line in res.stdout.splitlines()[1:]:
                        parts = line.split()
                        if len(parts) < 2:
                            continue
                        app_id = parts[0]
                        if app_id in {"core", "core18", "core20", "core22", "core24", "snapd"}:
                            continue
                        if self._is_system_component(app_id, app_id):
                            continue
                        apps.append(self._app_record(app_id, app_id, 0, "N/A", "Snap"))
            except (OSError, subprocess.SubprocessError):
                pass

        self.apps = sorted(apps, key=lambda x: x["size_bytes"], reverse=True)
        return self.apps

    def find_residue_paths(self, app_id: str, app_name: str, app_type: str) -> list[Path]:
        """Finds all data/config/cache paths associated with an app."""
        if self._requires_official_only_uninstall(app_id, app_name):
            return []

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
        app_name = str(app.get("name") or app.get("id") or "unknown")
        session_command = f"uninstall {app_name}"
        record_history_session(session_command, "started")
        package_status = "failed"
        package_event_recorded = False
        package_mode = str(app.get("type", "package")).lower()
        package_size = int(app.get("size_bytes") or 0)

        try:
            # 1. Graceful Kill (SIGTERM -> Wait -> SIGKILL)
            all_process_names = [app["id"], app["name"].lower()]
            if app["type"] == "Flatpak":
                import contextlib
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    system.run_command(["flatpak", "kill", app["id"]], capture=True, timeout=20)

            for proc in all_process_names:
                try:
                    res = system.run_command(["pgrep", "-x", proc], capture=True, timeout=5)
                    if res.ok:
                        # Step 1: SIGTERM
                        system.run_command(["pkill", "-15", "-x", proc], capture=True, timeout=5)
                        # Step 2: Brief wait
                        time.sleep(1.0)
                        # Step 3: Check and SIGKILL if still running
                        if system.run_command(["pgrep", "-x", proc], capture=True, timeout=5).ok:
                            system.run_command(["pkill", "-9", "-x", proc], capture=True, timeout=5)
                            time.sleep(0.5)
                except (OSError, subprocess.SubprocessError):
                    pass

            # 2. Binary uninstall
            if app["type"] == "Flatpak":
                res = system.run_command(["flatpak", "uninstall", "-y", app["id"]], capture=True)
            elif app["type"] == "Snap":
                res = system.run_command(
                    ["snap", "remove", app["id"]], use_sudo=True, capture=True
                )
            elif app["type"] == "APT":
                res = system.run_command(
                    ["apt", "remove", "-y", app["id"]], use_sudo=True, capture=True
                )
            elif app["type"] == "Pacman":
                res = system.run_command(
                    ["pacman", "-Rns", "--noconfirm", app["id"]],
                    use_sudo=True,
                    capture=True,
                )
            else:
                res = system.run_command(
                    ["dnf", "remove", "-y", app["id"]], use_sudo=True, capture=True
            )
            package_status = "removed" if res.ok else "failed"
            record_deletion_audit(app["id"], package_mode, package_status, package_size)
            package_event_recorded = True

            # 3. Path removal: explicit uninstall may remove app-owned data,
            # but hard-protected credentials/system paths remain blocked.
            removed_details = []
            for p in paths:
                success, _ = safe_remove(p, use_trash=False, allow_app_data_removal=True)
                try:
                    removed_details.append((success, str(p.relative_to(Path.home()))))
                except ValueError:
                    removed_details.append((success, str(p)))

            return removed_details
        finally:
            if package_status == "failed" and not package_event_recorded:
                record_deletion_audit(app.get("id", app_name), package_mode, "failed", package_size)
            record_history_session(session_command, "ended")


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
                    if system.run_command(["pgrep", "-x", proc], capture=True, timeout=5).ok:
                        is_running = True
                        break
                except (OSError, subprocess.SubprocessError):
                    pass
            app_paths = manager.find_residue_paths(app["id"], app["name"], app["type"])
            all_targets.append((app, app_paths, is_running))
            total_estimated_size += app["size_bytes"]

        confirmed = False
        with Navigator.raw_mode() as fd:
            preview_done = False
            while not preview_done:
                buf = ["\033[H"]  # Go home
                # Use \033[K on every line, including the very first line and spacers
                buf.append(f"\033[1;35m➔\033[0m {BOLD}Uninstallation Preview{RESET}\033[K\n")
                buf.append("\033[K\n")

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

                buf.append("\033[K\n")
                buf.append("\033[K\n")  # Explicit cleared spacer line
                app_text = "application" if len(selected_apps) == 1 else "applications"
                size_display = bytes_to_human(total_estimated_size)
                prompt = (
                    f" Remove {len(selected_apps)} {app_text}, {size_display} "
                    f" {GREEN}Enter{RESET} confirm, {GRAY}Space{RESET} cancel: "
                )
                buf.append(prompt + "\033[K")
                buf.append("\033[J")  # Clear remaining old lines below

                sys.stdout.write("".join(buf))
                sys.stdout.flush()

                # Capture key using standardized navigator with persistent raw mode
                ch = Navigator.get_key(fd)

                if ch in Navigator.ENTER:
                    confirmed = True
                    preview_done = True
                elif ch == Navigator.SPACE:
                    preview_done = True
                elif ch == Navigator.ESC and len(ch) == 1:
                    # Explicit ESC: Cancel and go back to list
                    preview_done = True
                else:
                    # MOUSE_EVENT, Arrows, or other keys: Stay on preview screen
                    continue

        if confirmed:
            print()
            # Ensure sudo session (require password) outside raw mode so sudo can own input.
            if not system.ensure_sudo_session(
                f"{MAGENTA}➔{RESET} App removal requires admin access\n"
                f"{MAGENTA}➔{RESET} Password: "
            ):
                if system.SUDO_CANCELLED:
                    print(f" {YELLOW}⚠️  Uninstall cancelled by user.{RESET}\n")
                else:
                    print(f" {RED}✗{RESET} Authorization failed. Uninstall cancelled.\n")
                return

            print(f" {GREEN}✓{RESET} Authorization successful.\n")

            # --- EXECUTION ---
            print(f"\n {GRAY}🚀 Processing...{RESET}")
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
            if not Navigator.wait_for_return("Press Enter to return application list , ESC to exit..."):
                return  # Exit uninstall completely
