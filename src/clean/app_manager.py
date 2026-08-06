import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..core import system
from ..core.constants import (
    BLUE,
    BOLD,
    CLEAR_SCREEN,
    GRAY,
    GREEN,
    MAGENTA,
    PURPLE,
    RED,
    RESET,
    THEME_TITLE,
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
            "xdg-desktop-portal-gtk",
            "xdg-desktop-portal-kde",
            "xdg-desktop-portal-wlr",
            "xdg-desktop-portal-lxqt",
            "xdg-user-dirs-gtk",
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
        return get_desktop_name(desktop_file) or name

    def _get_app_keywords(self, desktop_file: Path) -> list[str]:
        """Extracts potential folder name keywords from Exec and Icon fields."""
        keywords = {name.lower() for name in get_desktop_exec_names(desktop_file)}
        icon_name = get_desktop_icon(desktop_file).lower()
        if icon_name:
            keywords.add(icon_name)
        return list(keywords)

    def _executable_names_from_desktop(self, app_id: str) -> set[str]:
        """Real executable (comm) names parsed from the app's .desktop Exec line.

        These are what actually appear in the process table, unlike the localized
        display name (which can never match `pkill -x`).
        """
        names: set[str] = set()
        if not app_id:
            return names
        desktop_paths = [
            Path(f"/usr/share/applications/{app_id}.desktop"),
            Path.home() / f".local/share/applications/{app_id}.desktop",
        ]
        for dp in desktop_paths:
            if not dp.exists():
                continue
            names.update(get_desktop_exec_names(dp))
        return names

    @staticmethod
    def _candidate_process_names(app: dict[str, Any], extra: set[str] | None = None) -> list[str]:
        """Plausible process (comm) names to terminate before removing an app.

        Uses the package/flatpak id and any executable names from .desktop, but
        NOT the localized display name: a name like "Telegram Desktop" can never
        match `pkill -x`, and a short display name could kill an unrelated process.
        """
        names: set[str] = set(extra or set())
        app_id = str(app.get("id") or "")
        if app_id:
            names.add(app_id)
            if "." in app_id:  # flatpak: org.gnome.Music -> music
                names.add(app_id.rsplit(".", 1)[-1].lower())
        return [n for n in names if n]

    def run_full_scan(self, *, use_cache: bool = False) -> list[dict[str, Any]]:
        """Scans for user-facing applications across Linux package managers."""
        cache_key = self._current_scan_cache_key()
        if use_cache and self.has_fresh_scan_cache():
            self.apps = [app.copy() for app in self._scan_cache_apps or []]
            return self.apps

        apps = []

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
            if desktop_files:
                batch_size = 500
                for i in range(0, len(desktop_files), batch_size):
                    batch = desktop_files[i : i + batch_size]
                    if shutil.which("rpm"):
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
                    elif shutil.which("dpkg-query"):
                        res = system.run_command(["dpkg-query", "-S", *batch], capture=True)
                        for line in res.stdout.splitlines():
                            if ":" in line:
                                user_app_packages.add(
                                    self._strip_package_arch(line.split(":", 1)[0].strip())
                                )
                    elif shutil.which("pacman"):
                        res = system.run_command(["pacman", "-Qo", *batch], capture=True)
                        for line in res.stdout.splitlines():
                            if " is owned by " in line:
                                owned = line.split(" is owned by ", 1)[1].split()
                                if owned:
                                    user_app_packages.add(owned[0])
        except (OSError, subprocess.SubprocessError, ValueError):
            pass

        # 2. DNF/RPM Scan - Filtered by user_app_packages
        if shutil.which("rpm"):
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

        # 3. APT/DEB Scan - Filtered by desktop packages or large packages
        if shutil.which("dpkg-query"):
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

                            apps.append(
                                self._app_record(
                                    app_id,
                                    app_id,
                                    size_bytes,
                                    bytes_to_human(size_bytes),
                                    "APT",
                                    install_time,
                                )
                            )
            except (OSError, subprocess.SubprocessError, ValueError):
                pass

        # 4. Pacman Scan - Filtered by desktop packages or large packages
        if shutil.which("pacman"):
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
                        revision = parts[2] if len(parts) >= 3 else ""
                        if app_id in {"core", "core18", "core20", "core22", "core24", "snapd"}:
                            continue
                        if self._is_system_component(app_id, app_id):
                            continue

                        # Estimate size and time
                        size_bytes = 0
                        size_str = "N/A"
                        install_time = 0

                        # Primary method: Check the actual .snap file (most accurate)
                        if revision:
                            snap_file = Path(f"/var/lib/snapd/snaps/{app_id}_{revision}.snap")
                            if snap_file.exists():
                                try:
                                    size_bytes = snap_file.stat().st_size
                                    size_str = bytes_to_human(size_bytes)
                                    install_time = int(snap_file.stat().st_mtime)
                                except OSError:
                                    pass

                        # Fallback: Check mount points
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

                        apps.append(
                            self._app_record(
                                app_id, app_id, size_bytes, size_str, "Snap", install_time
                            )
                        )
            except (OSError, subprocess.SubprocessError):
                pass

        # 7. NPM Global Packages Scan
        if shutil.which("npm"):
            try:
                res = system.run_command(
                    ["npm", "list", "-g", "--json", "--depth=0"], capture=True, timeout=30
                )
                if res.ok and res.stdout:
                    data = json.loads(res.stdout)
                    dependencies = data.get("dependencies", {})
                    system_npm_pkgs = {
                        "npm",
                        "corepack",
                        "yarn",
                        "pnpm",
                        "npx",
                        "cnpm",
                    }
                    # Get global node_modules root path as fallback
                    npm_root_path = None
                    res_root = system.run_command(["npm", "root", "-g"], capture=True, timeout=5)
                    if res_root.ok and res_root.stdout.strip():
                        npm_root_path = Path(res_root.stdout.strip())

                    for pkg_name, info in dependencies.items():
                        clean_name = pkg_name.split("/")[-1]
                        if clean_name in system_npm_pkgs:
                            continue
                        if self._is_system_component(clean_name, pkg_name):
                            continue

                        pkg_path = info.get("path")
                        if not pkg_path and npm_root_path:
                            pkg_path = str(npm_root_path / pkg_name)

                        size_bytes = 0
                        install_time = 0
                        if pkg_path:
                            p = Path(pkg_path)
                            if p.exists():
                                try:
                                    install_time = int(p.stat().st_mtime)
                                    size_bytes = get_size_fast(p)
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
            except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError):
                pass

        # 8. Standalone CLI & Native Tools Dynamic Generic Scan
        # Scans for non-packaged CLI tools installed via single-file/script installers (e.g. Claude, Ollama, Cargo, Bun, Deno, Nvm, UV)
        home = Path.home()
        discovered_standalone: list[dict[str, Any]] = []

        # System/user systemd/env tokens to avoid misidentifying
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

        # Pattern A: ~/.local/bin/<cmd> with matching ~/.local/share/<cmd> or ~/.config/<cmd>
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
                    target_dir = (
                        share_dir
                        if share_dir.is_dir()
                        else (config_dir if config_dir.is_dir() else None)
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

        # Pattern B: Independent Home dot-directories with bin/ (e.g. ~/.bun, ~/.deno, ~/.cargo, ~/.nvm, ~/.kimi-code)
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
                if bin_path.is_symlink() and bin_path.exists():
                    size_bytes += bin_path.lstat().st_size
            except OSError:
                pass

            size_str = bytes_to_human(size_bytes) if size_bytes > 0 else "N/A"
            apps.append(
                self._app_record(
                    tool_id,
                    tool_name,
                    size_bytes,
                    size_str,
                    "CLI",
                    install_time,
                )
            )

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

    def find_residue_paths(self, app_id: str, app_name: str, app_type: str) -> list[Path]:
        """Finds all data/config/cache paths associated with an app."""
        if self._requires_official_only_uninstall(app_id, app_name):
            return []

        paths = []
        home_path = Path.home()
        seen = set()

        # 1. Standard XDG & Home CLI paths
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
        if "/" in app_id:
            parts = [p.strip("@").lower() for p in app_id.split("/") if p.strip("@")]
            targets.update(parts)

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
            with contextlib.suppress(OSError):
                for icon_file in icon_root.rglob("*"):
                    if not icon_file.is_file():
                        continue
                    file_lower = icon_file.name.lower()
                    if (
                        any(self._name_matches(file_lower, t) for t in targets)
                        and str(icon_file) not in seen
                    ):
                        paths.append(icon_file)
                        seen.add(str(icon_file))

        # 6. Systemd User Services (~/.config/systemd/user/)
        systemd_user_dir = home_path / ".config/systemd/user"
        if systemd_user_dir.exists():
            with contextlib.suppress(OSError):
                for service_file in systemd_user_dir.glob("*.service"):
                    file_lower = service_file.name.lower()
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
            try:
                # Only scan top-level dirs in home for speed/safety
                with os.scandir(home_path) as it:
                    for entry in it:
                        if not entry.is_dir() or not entry.name.startswith("."):
                            continue
                        entry_lower = entry.name.lower()
                        if entry_lower in protected_dir_names:
                            continue
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
            # 1. Graceful Kill (SIGTERM -> Wait -> SIGKILL). Use real executable
            # names (id + .desktop Exec), never the localized display name.
            all_process_names = self._candidate_process_names(
                app, self._executable_names_from_desktop(str(app.get("id") or ""))
            )
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
                res = system.run_command(
                    ["dnf", "remove", "-y", app["id"]], use_sudo=True, capture=True
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
            is_running = False
            for proc in manager._candidate_process_names(
                app, manager._executable_names_from_desktop(str(app.get("id") or ""))
            ):
                try:
                    if system.run_command(["pgrep", "-x", proc], capture=True, timeout=5).ok:
                        is_running = True
                        break
                except (OSError, subprocess.SubprocessError):
                    pass
            app_paths = manager.find_residue_paths(app["id"], app["name"], app["type"])
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
                        "Press Enter to return to application list, ESC to exit..."
                    ):
                        return
                    continue
                else:
                    print(f" {RED}✗{RESET} Authorization failed. Uninstall cancelled.\n")
                    return

            print(f" {GREEN}✓{RESET} Authorization successful.\n")

            # --- EXECUTION ---
            print(f"\n {GRAY}🚀 Processing...{RESET}\n")
            removed_names = []
            failed_names = []
            total_freed_all = 0
            has_apt = False

            for app, paths, _ in all_targets:
                print(f"  {PURPLE}➔{RESET} Removing {BOLD}{app['name']}{RESET}...")
                if app["type"] == "APT":
                    has_apt = True
                result = manager.execute_uninstall(app, paths)
                package_removed = bool(result.get("package_removed"))
                paths_removed = any(ok for ok, _ in result.get("removed_paths", []))
                if package_removed or paths_removed:
                    removed_names.append(app["name"])
                    if package_removed:
                        total_freed_all += app["size_bytes"]
                else:
                    failed_names.append(app["name"])

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
                "Press Enter to return to application list, ESC to exit..."
            ):
                return  # Exit uninstall completely
