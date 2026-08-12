import contextlib
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..core import system
from ..core.constants import (
    BLUE,
    BOLD,
    CLEAR_SCREEN,
    CYAN,
    GRAY,
    GREEN,
    MAGENTA,
    PURPLE,
    RED,
    RESET,
    RPM_QUERY_BATCH_SIZE,
    THEME_TITLE,
    WHITE,
    YELLOW,
)
from ..core.desktop_entry import get_desktop_exec_names, get_desktop_icon, get_desktop_name
from ..core.file_ops import (
    bytes_to_human,
    get_size_fast,
    parse_size_to_bytes,
    record_deletion_audit,
    safe_remove,
)
from ..core.history import record_history_session
from ..core.scan_cache import ScanCache
from ..core.text import sanitize_for_display
from ..core.whitelist import LINUX_USER_DATA_DIRS
from ..ui.navigator import Navigator, UninstallPreviewSelector, UninstallSelector


class UninstallManager:
    _SCAN_CACHE_TTL_SECONDS = 30
    _scan_cache_apps: list[dict[str, Any]] | None = None
    _scan_cache_time = 0.0
    _scan_cache_key: tuple[Any, ...] | None = None

    # Tokens too short or generic to safely substring-match against folder names.
    # Matching these loosely would flag unrelated directories for deletion
    # (e.g. "desktop" from "org.telegram.desktop", or "data"/"app").
    _GENERIC_TOKENS = frozenset(
        {
            "app",
            "apps",
            "data",
            "core",
            "bin",
            "cache",
            "config",
            "share",
            "gui",
            "lib",
            "tmp",
            "temp",
            "default",
            "common",
            "main",
            "client",
            "desktop",
            "system",
            "settings",
            "local",
            "user",
            "code",
            "go",
            "id",
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
            "cldr-emoji",
            "cinnamon",
            "dracut",
            "evolution-data-server",
            "firmware",
            "fontconfig",
            "gcc",
            "g++",
            "gcr",
            "geoclue",
            "glibc",
            "gnome-bluetooth",
            "gnome-control-center",
            "gnome-session",
            "gnome-settings-daemon",
            "gnome-shell",
            "gnome-software",
            "gnome-user-share",
            "grub",
            "ibus",
            "openjdk",
            "kernel",
            "kmod",
            "kwin",
            "langpack",
            "linux-headers",
            "linux-image",
            "llvm",
            "malcontent",
            "mesa",
            "mutter",
            "networkmanager",
            "nvidia-driver",
            "pipewire",
            "plasma",
            "pulseaudio",
            "qemu-common",
            "rust-std",
            "rygel",
            "selinux",
            "systemd",
            "tecla",
            "wayland",
            "wireplumber",
            "xdg-user-dirs",
            "xfce",
            "xorg",
        }
    )
    _SYSTEM_COMPONENT_EXACT_IDS = frozenset(
        {
            "baobab",
            "dconf",
            "decibels",
            "gdm",
            "gnome-boxes",
            "gnome-browser-connector",
            "gnome-calculator",
            "gnome-calendar",
            "gnome-characters",
            "gnome-clocks",
            "gnome-color-manager",
            "gnome-contacts",
            "gnome-connections",
            "gnome-disk-utility",
            "gnome-font-viewer",
            "gnome-initial-setup",
            "gnome-logs",
            "gnome-maps",
            "gnome-online-accounts",
            "gnome-system-monitor",
            "gnome-terminal",
            "gnome-text-editor",
            "gnome-tour",
            "gnome-tweaks",
            "gnome-weather",
            "gvfs",
            "libreoffice-core",
            "libreoffice-xsltfilter",
            "loupe",
            "mediawriter",
            "nautilus",
            "orca",
            "org.gnome.characters",
            "papers",
            "papers-previewer",
            "ptyxis",
            "showtime",
            "simple-scan",
            "eog",
            "evince",
            "file-roller",
            "gedit",
            "gnome-mahjongg",
            "gnome-mines",
            "gnome-sudoku",
            "gnome-system-log",
            "gnome-user-docs",
            "language-selector-gnome",
            "remmina",
            "seahorse",
            "software-properties-gtk",
            "totem",
            "ubuntu-desktop",
            "ubuntu-desktop-minimal",
            "ubuntu-docs",
            "ubuntu-drivers-common",
            "ubuntu-minimal",
            "ubuntu-release-upgrader-gtk",
            "ubuntu-session",
            "ubuntu-standard",
            "usb-creator-gtk",
            "xdg-desktop-portal-ubuntu",
            "xdg-desktop-portal",
            "xdg-desktop-portal-gnome",
            "xdg-desktop-portal-gtk",
            "xdg-desktop-portal-kde",
            "xdg-desktop-portal-wlr",
            "xdg-desktop-portal-lxqt",
            "xdg-user-dirs-gtk",
            "snapshot",
            "gnome-snapshot",
            "org.gnome.Snapshot",
            "org.gnome.snapshot",
            "cheese",
            "yelp",
        }
    )

    def __init__(self):
        self.apps: list[dict[str, Any]] = []

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
            bool(shutil.which("rpm")),
            bool(shutil.which("dpkg-query")),
            bool(shutil.which("pacman")),
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
        if app_id_lower in cls._SYSTEM_COMPONENT_EXACT_IDS or any(
            token in text for token in cls._SYSTEM_COMPONENT_TOKENS
        ):
            return True

        # Generic System Heuristics for non-app packages:
        # Prevent non-GUI libraries, development headers, static archives from leaking
        sys_suffixes = (
            "-libs",
            "-devel",
            "-dev",
            "-static",
            "-headers",
            "-plugins",
            "-modules",
        )
        if app_id_lower.startswith("libreoffice"):
            return any(app_id_lower.endswith(s) for s in sys_suffixes)

        sys_prefixes = ("lib", "gsettings-", "desktop-file-", "shared-mime-")
        return any(app_id_lower.endswith(s) for s in sys_suffixes) or any(
            app_id_lower.startswith(p) for p in sys_prefixes
        )

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

    def _get_app_keywords(self, desktop_file: Path) -> list[str]:
        """Extracts potential folder name keywords from Exec and Icon fields."""
        keywords = {name.lower() for name in get_desktop_exec_names(desktop_file)}
        icon_name = get_desktop_icon(desktop_file).lower()
        if icon_name:
            keywords.add(icon_name)
        return list(keywords)

    def _candidate_process_names(
        self, app: dict[str, Any], paths: list[Path] | None = None
    ) -> list[str]:
        """Plausible process (comm) names to terminate before removing an app.

        Dynamically discovers process names using:
        1. Package/Flatpak/Snap ID
        2. Binary names parsed from all associated .desktop Exec fields
        3. Active PIDs occupying the app's residue directories via fuser
        4. Name tokens (splitting hyphens/underscores/prefixes)
        """
        names: set[str] = set()
        app_id = str(app.get("id") or "")
        app_name = str(app.get("name") or "")

        if app_id:
            names.add(app_id)
            names.add(app_id.lower())
            if "." in app_id:  # flatpak: org.gnome.Music -> music
                names.add(app_id.rsplit(".", 1)[-1].lower())

        # Generic token splitting: e.g. "google-chrome-stable" -> "chrome", "linuxqq" -> "qq"
        for raw in (app_id, app_name):
            if not raw or " " in raw:
                continue
            lower_raw = raw.lower()
            for prefix in ("linux", "org.", "com.", "net.", "io.", "io.github."):
                if lower_raw.startswith(prefix) and len(lower_raw) > len(prefix) + 2:
                    names.add(lower_raw[len(prefix) :])
            for part in lower_raw.replace("_", "-").split("-"):
                if len(part) >= 3 and part not in (
                    "stable",
                    "beta",
                    "dev",
                    "desktop",
                    "linux",
                    "free",
                    "community",
                ):
                    names.add(part)

        # Dynamic .desktop Exec binary extraction
        desktop_dirs = [
            Path("/usr/share/applications"),
            Path.home() / ".local/share/applications",
            Path("/var/lib/flatpak/exports/share/applications"),
            Path.home() / ".local/share/flatpak/exports/share/applications",
        ]
        for ddir in desktop_dirs:
            if not ddir.is_dir():
                continue
            with contextlib.suppress(OSError):
                for entry in ddir.glob("*.desktop"):
                    entry_name = entry.name.lower()
                    if any(
                        t in entry_name for t in (app_id.lower(), app_name.lower()) if len(t) >= 2
                    ):
                        names.update(get_desktop_exec_names(entry))

        # Dynamic fuser / lsof inspection on app residue paths
        if paths:
            for p in paths:
                if p.exists():
                    try:
                        res = system.run_command(["fuser", str(p)], capture=True, timeout=3)
                        stdout_text = str(res.stdout or "")
                        if res.ok and stdout_text.strip():
                            # fuser outputs PIDs like '1234m'; extract pure numeric PIDs
                            for pid_clean in re.findall(r"\b\d+\b", stdout_text):
                                comm_path = Path(f"/proc/{pid_clean}/comm")
                                if comm_path.exists():
                                    with contextlib.suppress(OSError):
                                        comm_name = comm_path.read_text().strip()
                                        if comm_name:
                                            names.add(comm_name)
                    except (OSError, subprocess.SubprocessError):
                        pass

        return [n for n in names if n]

    def _pre_scan_package_desktop_names(self) -> tuple[set[str], dict[str, str]]:
        """Pre-scans native packages that provide desktop files and maps display names."""
        user_app_packages: set[str] = set()
        package_desktop_names: dict[str, str] = {}
        try:
            desktop_dirs = [
                "/usr/share/applications",
                str(Path.home() / ".local/share/applications"),
            ]
            desktop_files = []
            for d in desktop_dirs:
                p = Path(d)
                if p.exists():
                    desktop_files.extend(list(p.glob("*.desktop")))

            if desktop_files:
                batch_size = RPM_QUERY_BATCH_SIZE
                str_desktop_files = [str(f) for f in desktop_files]
                for i in range(0, len(str_desktop_files), batch_size):
                    batch_str = str_desktop_files[i : i + batch_size]
                    batch_paths = desktop_files[i : i + batch_size]
                    if shutil.which("rpm"):
                        res = system.run_command(
                            ["rpm", "-qf", "--queryformat", "%{NAME}\n"] + batch_str,
                            capture=True,
                            timeout=60,
                        )
                        if res.stdout:
                            lines = res.stdout.splitlines()
                            for df_path, line in zip(batch_paths, lines, strict=False):
                                line_str = line.strip()
                                if line_str and not line_str.startswith("file "):
                                    pkg = line_str
                                    user_app_packages.add(pkg)
                                    if pkg not in package_desktop_names:
                                        dname = get_desktop_name(df_path)
                                        if dname:
                                            package_desktop_names[pkg] = dname
                    if shutil.which("dpkg-query"):
                        res = system.run_command(["dpkg-query", "-S", *batch_str], capture=True)
                        for line in res.stdout.splitlines():
                            if ":" in line:
                                parts = line.split(":", 1)
                                pkg = self._strip_package_arch(parts[0].strip())
                                user_app_packages.add(pkg)
                                if pkg not in package_desktop_names:
                                    df_path = Path(parts[1].strip())
                                    if df_path.exists():
                                        dname = get_desktop_name(df_path)
                                        if dname:
                                            package_desktop_names[pkg] = dname
                    if shutil.which("pacman"):
                        res = system.run_command(["pacman", "-Qo", *batch_str], capture=True)
                        for line in res.stdout.splitlines():
                            if " is owned by " in line:
                                df_str, owned_str = line.split(" is owned by ", 1)
                                owned = owned_str.split()
                                if owned:
                                    pkg = owned[0]
                                    user_app_packages.add(pkg)
                                    if pkg not in package_desktop_names:
                                        df_path = Path(df_str.strip())
                                        if df_path.exists():
                                            dname = get_desktop_name(df_path)
                                            if dname:
                                                package_desktop_names[pkg] = dname
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return user_app_packages, package_desktop_names

    def _scan_rpm_packages(
        self, user_app_packages: set[str], package_desktop_names: dict[str, str]
    ) -> list[dict[str, Any]]:
        if not shutil.which("rpm"):
            return []
        apps = []
        try:
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
                        if self._is_system_component(app_id, app_id):
                            continue
                        if app_id in user_app_packages or size_bytes > 100 * 1024 * 1024:
                            display_name = package_desktop_names.get(app_id, app_id)
                            apps.append(
                                self._app_record(
                                    app_id,
                                    display_name,
                                    size_bytes,
                                    bytes_to_human(size_bytes),
                                    "DNF",
                                    install_time,
                                )
                            )
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return apps

    def _scan_apt_packages(
        self, user_app_packages: set[str], package_desktop_names: dict[str, str]
    ) -> list[dict[str, Any]]:
        if not shutil.which("dpkg-query"):
            return []
        apps = []
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
                        install_time = 0
                        list_file = Path(f"/var/lib/dpkg/info/{app_id}.list")
                        if list_file.exists():
                            with contextlib.suppress(OSError):
                                install_time = int(list_file.stat().st_mtime)

                        display_name = package_desktop_names.get(app_id, app_id)
                        apps.append(
                            self._app_record(
                                app_id,
                                display_name,
                                size_bytes,
                                bytes_to_human(size_bytes),
                                "APT",
                                install_time,
                            )
                        )
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        return apps

    def _scan_pacman_packages(
        self, user_app_packages: set[str], package_desktop_names: dict[str, str]
    ) -> list[dict[str, Any]]:
        if not shutil.which("pacman"):
            return []
        apps = []
        try:
            res = system.run_command(["pacman", "-Qi"], capture=True, timeout=60)
            if res.ok:
                package: dict[str, str] = {}
                for line in [*res.stdout.splitlines(), ""]:
                    if not line.strip():
                        app_id = package.get("Name", "").strip()
                        size_bytes = parse_size_to_bytes(package.get("Installed Size", ""))
                        if (
                            app_id
                            and not self._is_system_component(app_id, app_id)
                            and (app_id in user_app_packages or size_bytes > 100 * 1024 * 1024)
                        ):
                            display_name = package_desktop_names.get(app_id, app_id)
                            apps.append(
                                self._app_record(
                                    app_id,
                                    display_name,
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
        return apps

    def _scan_flatpak_apps(self) -> list[dict[str, Any]]:
        if not shutil.which("flatpak"):
            return []
        apps = []
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
                        install_time = 0
                        try:
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

                        size_bytes = parse_size_to_bytes(size_str)
                        apps.append(
                            self._app_record(
                                app_id, app_name, size_bytes, size_str, "Flatpak", install_time
                            )
                        )
        except (OSError, subprocess.SubprocessError):
            pass
        return apps

    def _scan_snap_apps(self, package_desktop_names: dict[str, str]) -> list[dict[str, Any]]:
        if not shutil.which("snap"):
            return []
        apps = []
        try:
            res = system.run_command(["snap", "list"], capture=True, timeout=60)
            if res.ok:
                for line in res.stdout.splitlines()[1:]:
                    parts = line.split()
                    if len(parts) < 2:
                        continue
                    app_id = parts[0]
                    revision = parts[2] if len(parts) >= 3 else ""
                    if app_id in {"core", "core18", "core20", "core22", "core24", "snapd"}:
                        continue
                    if self._is_system_component(app_id, app_id):
                        continue

                    size_bytes = 0
                    size_str = "N/A"
                    install_time = 0

                    if revision:
                        snap_file = Path(f"/var/lib/snapd/snaps/{app_id}_{revision}.snap")
                        if snap_file.exists():
                            try:
                                size_bytes = snap_file.stat().st_size
                                size_str = bytes_to_human(size_bytes)
                                install_time = int(snap_file.stat().st_mtime)
                            except OSError:
                                pass

                    if size_bytes == 0:
                        for mount_root in ["/snap", "/var/lib/snapd/snap"]:
                            snap_path = Path(f"{mount_root}/{app_id}/current")
                            if snap_path.exists():
                                try:
                                    if install_time == 0:
                                        install_time = int(snap_path.stat().st_mtime)
                                    res_size = system.run_command(
                                        ["du", "-sk", str(snap_path)], capture=True, timeout=5
                                    )
                                    if res_size.ok and res_size.stdout:
                                        kb = int(res_size.stdout.split()[0])
                                        size_bytes = kb * 1024
                                        size_str = bytes_to_human(size_bytes)
                                        break
                                except (OSError, ValueError, IndexError):
                                    pass

                    display_name = package_desktop_names.get(app_id, app_id)
                    apps.append(
                        self._app_record(
                            app_id, display_name, size_bytes, size_str, "Snap", install_time
                        )
                    )
        except (OSError, subprocess.SubprocessError):
            pass
        return apps

    def _scan_npm_global_packages(self) -> list[dict[str, Any]]:
        if not shutil.which("npm"):
            return []
        apps = []
        try:
            system_npm_pkgs = {"npm", "corepack", "yarn", "pnpm", "npx", "cnpm"}
            npm_roots = [
                Path.home() / ".npm-global" / "lib" / "node_modules",
                Path("/usr/lib/node_modules"),
                Path("/usr/local/lib/node_modules"),
            ]
            seen_npm_pkgs = set()
            for npm_modules_dir in npm_roots:
                if not npm_modules_dir.is_dir():
                    continue
                try:
                    for item in npm_modules_dir.iterdir():
                        pkg_dirs = []
                        if item.name.startswith("@") and item.is_dir():
                            with contextlib.suppress(OSError):
                                pkg_dirs.extend([sub for sub in item.iterdir() if sub.is_dir()])
                        elif item.is_dir():
                            pkg_dirs.append(item)

                        for pkg_path in pkg_dirs:
                            pkg_name = (
                                f"{item.name}/{pkg_path.name}"
                                if item.name.startswith("@")
                                else pkg_path.name
                            )
                            if pkg_name in seen_npm_pkgs:
                                continue
                            clean_name = pkg_path.name
                            if clean_name in system_npm_pkgs:
                                continue
                            if self._is_system_component(clean_name, pkg_name):
                                continue

                            seen_npm_pkgs.add(pkg_name)
                            size_bytes = 0
                            install_time = 0
                            try:
                                install_time = int(pkg_path.stat().st_mtime)
                                size_bytes = get_size_fast(pkg_path)
                            except OSError:
                                pass

                            size_str = bytes_to_human(size_bytes) if size_bytes > 0 else "N/A"
                            apps.append(
                                self._app_record(
                                    pkg_name,
                                    clean_name,
                                    size_bytes,
                                    size_str,
                                    "NPM",
                                    install_time,
                                )
                            )
                except OSError:
                    pass
        except OSError:
            pass
        return apps

    def _scan_standalone_cli_apps(self) -> list[dict[str, Any]]:
        home = Path.home()
        discovered_standalone: list[dict[str, Any]] = []
        skip_cli_names = {
            "pip",
            "python",
            "python3",
            "git",
            "bash",
            "zsh",
            "topo",
            "node",
            "npm",
            "yarn",
            "systemd",
        }
        cli_dir_aliases = {
            "agy": [home / ".gemini"],
        }

        local_bin = home / ".local/bin"
        if local_bin.is_dir():
            try:
                for entry in local_bin.iterdir():
                    cmd_name = entry.name.lower()
                    if cmd_name in skip_cli_names or cmd_name.startswith("."):
                        continue
                    if self._is_system_component(cmd_name, cmd_name):
                        continue

                    share_dir = home / ".local/share" / cmd_name
                    config_dir = home / ".config" / cmd_name
                    dot_home_dir = home / f".{cmd_name}"
                    alias_dirs = [d for d in cli_dir_aliases.get(cmd_name, []) if d.is_dir()]

                    target_dir = (
                        share_dir
                        if share_dir.is_dir()
                        else (
                            config_dir
                            if config_dir.is_dir()
                            else (
                                dot_home_dir
                                if dot_home_dir.is_dir()
                                else (alias_dirs[0] if alias_dirs else None)
                            )
                        )
                    )

                    if target_dir:
                        discovered_standalone.append(
                            {
                                "id": cmd_name,
                                "name": entry.name,
                                "binary": entry,
                                "install_dir": target_dir,
                            }
                        )
            except OSError:
                pass

        try:
            for item in home.iterdir():
                if item.name.startswith(".") and item.is_dir() and not item.is_symlink():
                    tool_name = item.name.lstrip(".")
                    if tool_name in skip_cli_names or len(tool_name) < 2:
                        continue
                    if self._is_system_component(tool_name, tool_name):
                        continue

                    bin_sub = item / "bin"
                    if bin_sub.is_dir():
                        short_name = tool_name.split("-")[0]
                        matching_bins = [
                            f
                            for f in bin_sub.iterdir()
                            if f.is_file()
                            and (
                                f.name.lower().startswith(tool_name) or f.name.lower() == short_name
                            )
                        ]
                        if matching_bins:
                            main_bin = matching_bins[0]
                            if not any(d["id"] == tool_name for d in discovered_standalone):
                                discovered_standalone.append(
                                    {
                                        "id": tool_name,
                                        "name": tool_name,
                                        "binary": main_bin,
                                        "install_dir": item,
                                    }
                                )
        except OSError:
            pass

        apps = []
        for tool in discovered_standalone:
            tool_id = str(tool["id"])
            tool_name = str(tool["name"])
            bin_path: Path = tool["binary"]
            inst_dir: Path = tool["install_dir"]

            size_bytes = 0
            install_time = 0
            try:
                install_time = int(inst_dir.stat().st_mtime)
                size_bytes = get_size_fast(inst_dir)
                if bin_path.exists():
                    size_bytes += (
                        bin_path.lstat().st_size
                        if bin_path.is_symlink()
                        else get_size_fast(bin_path)
                    )
            except OSError:
                pass

            size_str = bytes_to_human(size_bytes) if size_bytes > 0 else "N/A"
            record = self._app_record(
                tool_id,
                tool_name,
                size_bytes,
                size_str,
                "CLI",
                install_time,
            )
            record["install_dir"] = inst_dir
            apps.append(record)

        return apps

    def _pre_scan_search_roots(self) -> dict[Path, list[tuple[str, Path]]]:
        """Pre-scans search_roots ONCE to avoid O(N) redundant disk scandirs."""
        home_path = Path.home()
        search_roots = [
            home_path / ".config",
            home_path / ".local/share",
            home_path / ".local/state",
            home_path / ".cache",
            home_path / ".var/app",
            home_path / "snap",
        ]
        pre_scanned_entries: dict[Path, list[tuple[str, Path]]] = {}
        for root in search_roots:
            if root.exists():
                try:
                    entries: list[tuple[str, Path]] = []
                    with os.scandir(root) as it:
                        for item_entry in it:
                            entries.append((item_entry.name.lower(), Path(item_entry.path)))
                    pre_scanned_entries[root] = entries
                except OSError:
                    pass
        for icon_root in (
            home_path / ".local/share/icons",
            home_path / ".local/share/pixmaps",
        ):
            if not icon_root.exists():
                continue
            entries = []
            with contextlib.suppress(OSError):
                for icon_file in icon_root.rglob("*"):
                    if icon_file.is_file():
                        entries.append((icon_file.name.lower(), icon_file))
            pre_scanned_entries[icon_root] = entries

        service_root = home_path / ".config/systemd/user"
        if service_root.exists():
            with contextlib.suppress(OSError):
                pre_scanned_entries[service_root] = [
                    (service.name.lower(), service) for service in service_root.glob("*.service")
                ]

        with contextlib.suppress(OSError), os.scandir(home_path) as home_entries:
            pre_scanned_entries[home_path] = [
                (entry.name.lower(), Path(entry.path))
                for entry in home_entries
                if entry.is_dir(follow_symlinks=False) and entry.name.startswith(".")
            ]
        return pre_scanned_entries

    def _calculate_app_sizes_and_residues(
        self, apps: list[dict[str, Any]], pre_scanned_entries: dict[Path, list[tuple[str, Path]]]
    ) -> None:
        """Calculates total app sizes including user data and cache residues in parallel."""

        def _process_single_app(app: dict[str, Any]):
            residue_paths = self.find_residue_paths(
                app["id"], app["name"], app["type"], pre_scanned_entries=pre_scanned_entries
            )
            inst_dir_val = app.get("install_dir")
            target_inst_dir: Path | None = Path(inst_dir_val).resolve() if inst_dir_val else None
            filtered_residue_paths = [
                p
                for p in residue_paths
                if target_inst_dir is None or Path(p).resolve() != target_inst_dir
            ]

            residue_size = sum(get_size_fast(p) for p in filtered_residue_paths)
            if residue_size > 0:
                app["size_bytes"] += residue_size
                app["size_str"] = bytes_to_human(app["size_bytes"])

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(_process_single_app, apps))

    def run_full_scan(self, *, use_cache: bool = False) -> list[dict[str, Any]]:
        """Scans for user-facing applications across Linux package managers in parallel."""
        cache_key = self._current_scan_cache_key()
        if use_cache and self.has_fresh_scan_cache():
            self.apps = [app.copy() for app in self._scan_cache_apps or []]
            return self.apps

        user_app_packages, package_desktop_names = self._pre_scan_package_desktop_names()

        scan_tasks = [
            lambda: self._scan_rpm_packages(user_app_packages, package_desktop_names),
            lambda: self._scan_apt_packages(user_app_packages, package_desktop_names),
            lambda: self._scan_pacman_packages(user_app_packages, package_desktop_names),
            lambda: self._scan_flatpak_apps(),
            lambda: self._scan_snap_apps(package_desktop_names),
            lambda: self._scan_npm_global_packages(),
            lambda: self._scan_standalone_cli_apps(),
        ]

        apps = []
        with ThreadPoolExecutor(max_workers=len(scan_tasks)) as executor:
            futures = [executor.submit(task) for task in scan_tasks]
            for future in as_completed(futures):
                with contextlib.suppress(Exception):
                    apps.extend(future.result())

        pre_scanned_entries = self._pre_scan_search_roots()
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

    def find_residue_paths(
        self,
        app_id: str,
        app_name: str,
        app_type: str,
        pre_scanned_entries: dict[Path, list[tuple[str, Path]]] | None = None,
    ) -> list[Path]:
        """Finds all data/config/cache paths associated with an app."""
        if self._requires_official_only_uninstall(app_id, app_name):
            return []

        paths = []
        home_path = Path.home()
        seen = set()

        # 1. Standard XDG, Flatpak & Snap paths
        search_roots = [
            home_path / ".config",
            home_path / ".local/share",
            home_path / ".local/state",
            home_path / ".cache",
            home_path / ".var/app",  # Flatpak
            home_path / "snap",  # Snap
        ]

        # 2. Common variants of the name
        targets = {app_id.lower(), app_name.lower()}
        if "." in app_id:
            targets.add(app_id.split(".")[-1].lower())
        if "/" in app_id:
            parts = [p.strip("@").lower() for p in app_id.split("/") if p.strip("@")]
            targets.update(parts)
        for t in list(targets):
            for sep in ("-", "_"):
                if sep in t:
                    prefix = t.rsplit(sep, 1)[0]
                    if len(prefix) >= 3 and prefix not in UninstallManager._GENERIC_TOKENS:
                        targets.add(prefix)

        # Check direct home hidden directories (e.g. ~/.claude, ~/.kimi, ~/.codex, ~/.grok, ~/.cloudbase)
        for t in list(targets):
            if len(t) >= 3:
                dot_dir = home_path / f".{t}"
                if dot_dir.is_dir() and str(dot_dir) not in seen:
                    paths.append(dot_dir)
                    seen.add(str(dot_dir))

        # 3. .desktop file keywords
        desktop_paths = [
            Path(f"/usr/share/applications/{app_id}.desktop"),
            home_path / f".local/share/applications/{app_id}.desktop",
        ]
        for dp in desktop_paths:
            if dp.exists():
                targets.update(self._get_app_keywords(dp))

        # 4. Search using pre-scanned index or fast scandir
        for root in search_roots:
            if not root.exists():
                continue
            if pre_scanned_entries and root in pre_scanned_entries:
                for entry_lower, p in pre_scanned_entries[root]:
                    for t in targets:
                        if self._name_matches(entry_lower, t) and str(p) not in seen:
                            paths.append(p)
                            seen.add(str(p))
            else:
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

        # 5. Local Desktop Launchers & Icons
        local_desktop = home_path / ".local/share/applications" / f"{app_id}.desktop"
        if local_desktop.exists() and str(local_desktop) not in seen:
            paths.append(local_desktop)
            seen.add(str(local_desktop))

        icon_roots = [
            home_path / ".local/share/icons",
            home_path / ".local/share/pixmaps",
        ]
        for icon_root in icon_roots:
            if not icon_root.exists():
                continue
            indexed_icons = pre_scanned_entries.get(icon_root) if pre_scanned_entries else None
            if indexed_icons is None:
                with contextlib.suppress(OSError):
                    indexed_icons = [
                        (icon.name.lower(), icon) for icon in icon_root.rglob("*") if icon.is_file()
                    ]
            for file_lower, icon_file in indexed_icons or []:
                if (
                    any(self._name_matches(file_lower, t) for t in targets)
                    and str(icon_file) not in seen
                ):
                    paths.append(icon_file)
                    seen.add(str(icon_file))

        # 6. Systemd User Services (~/.config/systemd/user/)
        systemd_user_dir = home_path / ".config/systemd/user"
        if systemd_user_dir.exists():
            indexed_services = (
                pre_scanned_entries.get(systemd_user_dir) if pre_scanned_entries else None
            )
            if indexed_services is None:
                with contextlib.suppress(OSError):
                    indexed_services = [
                        (service.name.lower(), service)
                        for service in systemd_user_dir.glob("*.service")
                    ]
            for file_lower, service_file in indexed_services or []:
                if (
                    any(self._name_matches(file_lower, t) for t in targets)
                    and str(service_file) not in seen
                ):
                    paths.append(service_file)
                    seen.add(str(service_file))

        # 7. Wine prefix check (optional, if wechat/etc)
        if "wechat" in app_name.lower():
            wine_p = home_path / ".xwechat"
            if wine_p.exists() and str(wine_p) not in seen:
                paths.append(wine_p)
                seen.add(str(wine_p))

        # 8. Deep Subdirectory Search (home-root residue).
        # Only HIDDEN dot-directories at the home root (e.g. ~/.someapp) are
        # considered here. Visible top-level home folders are user workspaces and
        # data — ~/Projects, ~/IdeaProjects, ~/studio-projects, ~/notes-backup,
        # ~/VirtualBox VMs — and must NEVER be matched as residue: a fuzzy name
        # hit there would permanently delete the user's own files. XDG user-data
        # dirs are excluded too (defence in depth; they are visible anyway).
        if len(app_name) > 3:
            protected_dir_names = {d.lower() for d in LINUX_USER_DATA_DIRS}
            indexed_home = pre_scanned_entries.get(home_path) if pre_scanned_entries else None
            if indexed_home is None:
                try:
                    with os.scandir(home_path) as it:
                        indexed_home = [
                            (entry.name.lower(), Path(entry.path))
                            for entry in it
                            if entry.is_dir(follow_symlinks=False) and entry.name.startswith(".")
                        ]
                except OSError:
                    indexed_home = []
            for entry_lower, entry_path in indexed_home:
                if entry_lower in protected_dir_names:
                    continue
                if (
                    self._name_matches(entry_lower, app_name.lower())
                    and str(entry_path) not in seen
                    and home_path in entry_path.parents
                ):
                    paths.append(entry_path)
                    seen.add(str(entry_path))

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
            # 1. Graceful Kill (SIGTERM -> Wait -> SIGKILL). Use real executable
            # names (id + .desktop Exec), never the localized display name.
            all_process_names = self._candidate_process_names(app, paths)
            if app["type"] == "Flatpak":
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    system.run_command(["flatpak", "kill", app["id"]], capture=True, timeout=20)

            # 1. Graceful Kill (Batched)
            processes_to_kill = []
            for proc in all_process_names:
                try:
                    if system.run_command(["pgrep", "-x", proc], capture=True, timeout=5).ok:
                        processes_to_kill.append(proc)
                except (OSError, subprocess.SubprocessError):
                    continue

            if processes_to_kill:
                for proc in processes_to_kill:
                    system.run_command(["pkill", "-15", "-x", proc], capture=True, timeout=5)

                time.sleep(1.0)

                for proc in processes_to_kill:
                    if system.run_command(["pgrep", "-x", proc], capture=True, timeout=5).ok:
                        system.run_command(["pkill", "-9", "-x", proc], capture=True, timeout=5)
                time.sleep(0.5)

            # 2. Binary uninstall
            if app["type"] == "Flatpak":
                res = system.run_command(["flatpak", "uninstall", "-y", app["id"]], capture=True)
            elif app["type"] == "Snap":
                res = system.run_command(
                    ["snap", "remove", "--purge", app["id"]], use_sudo=True, capture=True
                )
            elif app["type"] == "NPM":
                res = system.run_command(
                    ["npm", "uninstall", "-g", app["id"]], capture=True, timeout=60
                )
                # Clean empty scoped directory in global node_modules (e.g. ~/.npm-global/lib/node_modules/@cloudbase)
                if "/" in app["id"]:
                    scope = app["id"].split("/")[0]
                    res_root = system.run_command(["npm", "root", "-g"], capture=True, timeout=5)
                    if res_root.ok and res_root.stdout.strip():
                        scope_dir = Path(res_root.stdout.strip()) / scope
                        if scope_dir.is_dir():
                            with contextlib.suppress(OSError):
                                if not any(scope_dir.iterdir()):
                                    scope_dir.rmdir()
            elif app["type"] == "CLI":
                # Remove standalone binary & install directory
                home_p = Path.home()
                cli_targets = [
                    home_p / ".local/bin" / app["id"],
                    home_p / ".local/share" / app["id"],
                    home_p / f".{app['id']}",
                ]
                for ct in cli_targets:
                    if ct.exists():
                        safe_remove(ct, use_trash=True, allow_app_data_removal=True)
                res = system.CommandResult(
                    args=["cli_uninstall"], returncode=0, stdout="CLI uninstalled"
                )
            elif app["type"] == "APT":
                res = system.run_command(
                    ["apt", "purge", "-y", app["id"]], use_sudo=True, capture=True
                )
            elif app["type"] == "Pacman":
                res = system.run_command(
                    ["pacman", "-Rns", "--noconfirm", app["id"]],
                    use_sudo=True,
                    capture=True,
                )
            else:
                dnf_cmd = "dnf5" if shutil.which("dnf5") else "dnf"
                res = system.run_command(
                    [dnf_cmd, "remove", "-y", app["id"]], use_sudo=True, capture=True
                )
            package_status = "removed" if res.ok else "failed"
            record_deletion_audit(app["id"], package_mode, package_status, package_size)
            package_event_recorded = True

            # 3. Path removal: explicit uninstall may remove app-owned data,
            # but hard-protected credentials/system paths remain blocked.
            removed_details = []
            removed_systemd_service = False
            for p in paths:
                # Residue removal is recoverable (trash) rather than a permanent
                # wipe: residue discovery is heuristic, so a mis-matched user
                # directory must be undoable. allow_app_data_removal still lets
                # app-owned data go, while hard-protected paths (whitelist,
                # credentials, system, XDG user-data dirs) stay blocked.
                success, _ = safe_remove(p, use_trash=True, allow_app_data_removal=True)
                if success and str(p).endswith(".service") and ".config/systemd/user" in str(p):
                    removed_systemd_service = True
                try:
                    removed_details.append((success, str(p.relative_to(Path.home()))))
                except ValueError:
                    removed_details.append((success, str(p)))

            if removed_systemd_service and shutil.which("systemctl"):
                system.run_command(
                    ["systemctl", "--user", "daemon-reload"], capture=True, timeout=10
                )

            return {
                "package_removed": package_status == "removed",
                "removed_paths": removed_details,
            }
        finally:
            if package_status == "failed" and not package_event_recorded:
                record_deletion_audit(app.get("id", app_name), package_mode, "failed", package_size)
            record_history_session(session_command, "ended")


def run_uninstall():
    manager = UninstallManager()

    while True:
        if not manager.has_fresh_scan_cache():
            sys.stdout.write(
                CLEAR_SCREEN + f"\n {THEME_TITLE}Select Application to Remove{RESET}\n\n"
                f" {GRAY}Scanning installed applications...{RESET}\n"
            )
            sys.stdout.flush()
        apps = manager.run_full_scan(use_cache=True)

        if not apps:
            print(f"\n   {RED}No applications found to uninstall.{RESET}")
            Navigator.wait_for_return()
            return

        selector = UninstallSelector("Select Application to Remove", apps)
        selected_indices = selector.run()

        if not selected_indices:
            return

        # Residue discovery and process checks touch the filesystem / spawn pgrep,
        # so compute them once before handing the preview data to the UI layer.
        selected_apps = [apps[i] for i in selected_indices]
        all_targets = []
        for app in selected_apps:
            app_paths = manager.find_residue_paths(app["id"], app["name"], app["type"])
            is_running = False
            for proc in manager._candidate_process_names(app, app_paths):
                try:
                    if system.run_command(["pgrep", "-x", proc], capture=True, timeout=5).ok:
                        is_running = True
                        break
                except (OSError, subprocess.SubprocessError):
                    pass
            all_targets.append((app, app_paths, is_running))

        confirmed = UninstallPreviewSelector(all_targets).run()

        if confirmed:
            # Ensure sudo session (require password) outside raw mode so sudo can own input.
            if not system.ensure_sudo_session(
                f"{MAGENTA}➔{RESET} App removal requires admin access\n{MAGENTA}➔{RESET} Password: "
            ):
                if system.SUDO_CANCELLED:
                    # Navigator.wait_for_return already adds a leading newline
                    print(f" {YELLOW}⚠️  Uninstall cancelled by user.{RESET}", end="")
                    if not Navigator.wait_for_return(
                        f"Press {GREEN}Enter{RESET} {WHITE}to return to application list{RESET}, {CYAN}ESC{RESET} {WHITE}to exit...{RESET}"
                    ):
                        return
                    continue
                else:
                    print(f" {RED}✗{RESET} Authorization failed. Uninstall cancelled.\n")
                    return

            print(f" {GREEN}✓{RESET} Authorization successful.\n")

            # --- EXECUTION ---
            print(f"\n {WHITE}🚀 Processing...{RESET}\n")
            removed_names = []
            failed_names = []
            total_freed_all = 0
            has_apt = False

            for app, paths, _ in all_targets:
                # `name` comes from a .desktop Name= field, so it is untrusted; these
                # lists feed the summary lines only, never a filesystem operation.
                safe_app_name = sanitize_for_display(str(app["name"]))
                print(f"  {PURPLE}➔{RESET} Removing {BOLD}{safe_app_name}{RESET}...")
                if app["type"] == "APT":
                    has_apt = True
                result = manager.execute_uninstall(app, paths)
                package_removed = bool(result.get("package_removed"))
                paths_removed = any(ok for ok, _ in result.get("removed_paths", []))
                if package_removed or paths_removed:
                    removed_names.append(safe_app_name)
                    if package_removed:
                        total_freed_all += app["size_bytes"]
                else:
                    failed_names.append(safe_app_name)

            if has_apt and removed_names:
                print(f"  {PURPLE}➔{RESET} Cleaning up orphaned dependencies...")
                system.run_command(["apt", "autoremove", "-y"], use_sudo=True, capture=True)

            # Final Summary — only report what actually succeeded.
            if removed_names:
                ScanCache.clear()
                UninstallManager.clear_scan_cache()
            print("=" * 70)
            print(f"{BLUE}Uninstall complete{RESET}")
            names_str = ", ".join(removed_names) if removed_names else "none"
            msg = f"Removed {len(removed_names)} app(s), freed {GREEN}"
            msg += f"{bytes_to_human(total_freed_all)}{RESET}: {names_str}"
            print(msg)
            if failed_names:
                print(f" {RED}✗ Failed:{RESET} {', '.join(failed_names)}")
            print("=" * 70)
            Navigator.play_delete()

            # Standardized return/exit prompt
            if not Navigator.wait_for_return(
                f"Press {GREEN}Enter{RESET} {WHITE}to return to application list{RESET}, {CYAN}ESC{RESET} {WHITE}to exit...{RESET}"
            ):
                return  # Exit uninstall completely
