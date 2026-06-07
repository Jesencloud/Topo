import json
from pathlib import Path

from .paths import get_config_dir


def get_whitelist_file() -> Path:
    return get_config_dir() / "whitelist.json"


# Paths that are always protected recursively
DEFAULT_CRITICAL_PATHS = [
    "/bin",
    "/boot",
    "/dev",
    "/etc",
    "/lib",
    "/lib64",
    "/proc",
    "/root",
    "/run",
    "/sbin",
    "/sys",
    "/usr",
    "/var",
]
CRITICAL_PREFIX_PATHS = tuple(Path(path) for path in DEFAULT_CRITICAL_PATHS)
LEGACY_SEEDED_WHITELIST_PATHS = frozenset({"/", *DEFAULT_CRITICAL_PATHS})

# Paths that are only protected from exact deletion
DELETION_CRITICAL_EXACT_PATHS = tuple(
    Path(path) for path in ("/", "/home", "/mnt", "/media", "/srv", "/usr", "/var", "/tmp", "/boot")
)

LINUX_BROWSER_PROFILE_PATHS = [
    # Firefox family
    ".mozilla",
    ".librewolf",
    ".floorp",
    ".waterfox",
    ".zen",
    # Chromium family
    ".config/google-chrome",
    ".config/google-chrome-beta",
    ".config/google-chrome-unstable",
    ".config/chromium",
    ".config/ungoogled-chromium",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    ".config/microsoft-edge-beta",
    ".config/microsoft-edge-dev",
    ".config/vivaldi",
    ".config/vivaldi-snapshot",
    ".config/opera",
    ".config/opera-beta",
    ".config/opera-developer",
    ".config/thorium",
    ".config/Thorium",
    ".config/yandex-browser",
    ".config/yandex-browser-beta",
]

LINUX_PROTECTED_HOME_PATHS = [
    # Credentials and encryption material
    ".ssh",
    ".gnupg",
    ".pki",
    ".password-store",
    ".local/share/keyrings",
    ".config/sops",
    ".config/age",
    # Browser profiles
    *LINUX_BROWSER_PROFILE_PATHS,
    ".thunderbird",
    # Messaging and social
    ".local/share/TelegramDesktop",
    ".config/Signal",
    ".config/discord",
    ".config/Slack",
    ".config/Element",
    ".config/whatsapp-for-linux",
    ".config/transmission",
    # Password managers and authenticators
    ".config/Bitwarden",
    ".config/1Password",
    ".config/keepassxc",
    ".config/KeePassXC",
    ".local/share/keepassxc",
    ".local/share/KeePassXC",
    ".config/authy-desktop",
    # Input methods and personal dictionaries
    ".config/fcitx",
    ".config/fcitx5",
    ".config/ibus",
    ".config/rime",
    ".config/Rime",
    ".config/uim",
    ".uim.d",
    ".local/share/fcitx",
    ".local/share/fcitx5",
    ".local/share/ibus",
    ".local/share/rime",
    ".local/share/uim",
    # Desktop environment and system settings
    ".config/dconf",
    ".config/gnome-session",
    ".config/gnome-shell",
    ".config/gnome-tweaks",
    ".config/gtk-2.0",
    ".config/gtk-3.0",
    ".config/gtk-4.0",
    ".config/nautilus",
    ".config/user-dirs.dirs",
    ".config/mimeapps.list",
    ".config/pulse",
    ".config/fontconfig",
    ".local/share/gnome-shell",
    ".local/share/gvfs-metadata",
    ".local/share/nautilus",
    ".local/share/flatpak",
    ".local/share/fonts",
    # Shell and CLI configs
    ".bashrc",
    ".bash_profile",
    ".bash_history",
    ".zshrc",
    ".zprofile",
    ".zsh_history",
    ".profile",
    ".config/fish",
    ".config/gh",
    ".config/gcloud",
    ".aws",
    ".kube",
    ".docker",
    ".azure",
    # Wallets and crypto tools
    ".electrum",
    ".config/Electrum",
    ".config/Exodus",
    ".config/Ledger Live",
    ".config/Trezor",
    # Database clients and workspaces
    ".local/share/DBeaverData",
    ".config/DBeaverData",
    ".pgadmin",
    ".config/pgadmin",
    ".config/JetBrains",
    ".local/share/JetBrains",
    # IDE/editor user config
    ".config/Code",
    ".config/Code - OSS",
    ".config/VSCodium",
    ".config/Cursor",
    ".config/zed",
    ".config/nvim",
    ".local/share/nvim",
    ".emacs.d",
    ".config/sublime-text",
    ".config/sublime-text-3",
    # Sync and cloud storage
    ".dropbox",
    ".config/Nextcloud",
    ".config/syncthing",
    ".config/rclone",
]

LINUX_HARD_PROTECTED_HOME_PATHS = [
    ".ssh",
    ".gnupg",
    ".pki",
    ".password-store",
    ".local/share/keyrings",
    ".config/sops",
    ".config/age",
    ".aws",
    ".kube",
    ".docker",
    ".config/gh",
]

# Standard XDG user-data directories. Protected as DIRECTORIES (exact match
# only) so uninstall residue cleanup can never delete ~/Music, ~/Videos,
# ~/Documents, etc. Files *inside* them stay deletable via Analyze.
LINUX_USER_DATA_DIRS = [
    "Desktop",
    "Documents",
    "Downloads",
    "Music",
    "Pictures",
    "Public",
    "Templates",
    "Videos",
]

LINUX_PROTECTED_FLATPAK_APP_IDS = [
    "app.zen_browser.zen",
    "com.github.Eloston.UngoogledChromium",
    "com.bitwarden.desktop",
    "com.brave.Browser",
    "com.google.Chrome",
    "com.google.ChromeDev",
    "com.microsoft.Edge",
    "com.microsoft.EdgeDev",
    "com.opera.Opera",
    "com.vivaldi.Vivaldi",
    "io.github.ungoogled_software.ungoogled_chromium",
    "io.gitlab.librewolf-community",
    "md.obsidian.Obsidian",
    "org.chromium.Chromium",
    "org.gnome.World.Secrets",
    "org.keepassxc.KeePassXC",
    "org.mozilla.firefox",
    "org.mozilla.Thunderbird",
    "org.pgadmin.pgadmin4",
    "org.telegram.desktop",
    "com.discordapp.Discord",
    "com.slack.Slack",
    "im.riot.Riot",
]

LINUX_CLEANABLE_APP_DATA_DIR_NAMES = frozenset(
    {
        "Cache",
        "Cache_Data",
        "cache",
        "cache2",
        "CacheStorage",
        "CachedData",
        "Code Cache",
        "component_crx_cache",
        "Crash Reports",
        "Crashpad",
        "DawnCache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
        "extensions_crx_cache",
        "GPUCache",
        "GraphiteDawnCache",
        "GrShaderCache",
        "jumpListCache",
        "logs",
        "Logs",
        "Media Cache",
        "OfflineCache",
        "ScriptCache",
        "Service Worker",
        "ShaderCache",
        "startupCache",
    }
)


def _ensure_config():
    config_dir = get_config_dir()
    whitelist_file = get_whitelist_file()
    if not config_dir.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
    if not whitelist_file.exists():
        with open(whitelist_file, "w") as f:
            # Seed with empty list; critical paths are hardcoded for safety
            json.dump([], f, indent=4)


def get_whitelist():
    _ensure_config()
    try:
        with open(get_whitelist_file()) as f:
            data = json.load(f)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [path for path in data if path not in LEGACY_SEEDED_WHITELIST_PATHS]


def add_to_whitelist(path_str: str):
    _ensure_config()
    path = Path(path_str).expanduser().resolve()
    current = get_whitelist()
    if str(path) not in current:
        current.append(str(path))
        with open(get_whitelist_file(), "w") as f:
            json.dump(current, f, indent=4)
        return True
    return False


def remove_from_whitelist(path_str: str):
    _ensure_config()
    path = Path(path_str).expanduser().resolve()
    current = get_whitelist()
    if str(path) in current:
        current.remove(str(path))
        with open(get_whitelist_file(), "w") as f:
            json.dump(current, f, indent=4)
        return True
    return False


def _resolve_path(path) -> Path:
    if not isinstance(path, Path):
        path = Path(path)

    try:
        return path.expanduser().resolve()
    except Exception:
        return path.absolute()


def _is_system_carve_out(path: Path) -> bool:
    path_str = str(path)
    return path_str.startswith(("/var/tmp/", "/var/cache/"))


def _is_critical_system_path(path: Path) -> bool:
    if path == Path("/") or path in DELETION_CRITICAL_EXACT_PATHS:
        return True

    for prefix in CRITICAL_PREFIX_PATHS:
        try:
            prefix_res = prefix.resolve()
        except Exception:
            prefix_res = prefix.absolute()

        if (path == prefix_res or prefix_res in path.parents) and not _is_system_carve_out(path):
            return True
    return False


def get_hard_protection_reason(path) -> str | None:
    """Return why a path is protected across every deletion context."""
    path = _resolve_path(path)

    if _is_critical_system_path(path):
        return "critical system path"

    try:
        home = Path.home().resolve()
        if path == home:
            return "home directory"
    except Exception:
        home = Path.home()

    # Protect standard XDG user-data directories themselves (exact match) from
    # every deletion context, including uninstall residue removal. Files *inside*
    # them remain deletable via Analyze, so this only blocks wiping the whole dir.
    for rel in LINUX_USER_DATA_DIRS:
        try:
            user_dir = (home / rel).resolve()
        except OSError:
            user_dir = (home / rel).absolute()
        if path == user_dir:
            return "user data directory"

    protected_home_paths = [home / rel for rel in LINUX_HARD_PROTECTED_HOME_PATHS]
    for protected in protected_home_paths:
        try:
            prot_path = protected.expanduser().resolve()
        except OSError:
            prot_path = protected.expanduser().absolute()
        if path == prot_path or prot_path in path.parents:
            return "credential or identity data"

    try:
        topo_config = get_config_dir().resolve()
        if path == topo_config or topo_config in path.parents:
            return "Topo configuration"
    except Exception:
        pass

    for prot_str in get_whitelist():
        try:
            prot_path = Path(prot_str).expanduser().resolve()
            if path == prot_path or prot_path in path.parents:
                return "user whitelist"
        except Exception:
            continue

    return None


def is_hard_protected(path) -> bool:
    """Return True for paths that no deletion mode may bypass."""
    return get_hard_protection_reason(path) is not None


def is_protected(path) -> bool:
    """Check if a path is protected by hard rules, app-data rules, or user whitelist."""
    path = _resolve_path(path)

    if is_hard_protected(path):
        return True

    if is_sensitive_linux_app_data(path):
        return not is_cleanable_linux_app_data(path)

    return False


def is_cleanable_linux_app_data(path: Path) -> bool:
    """Return True for cache-like paths inside otherwise sensitive Linux app data."""
    path = _resolve_path(path)

    if is_hard_protected(path):
        return False
    if not is_sensitive_linux_app_data(path):
        return False

    try:
        home = Path.home().resolve()
        rel_parts = path.relative_to(home).parts
    except (OSError, ValueError):
        return False

    return any(part in LINUX_CLEANABLE_APP_DATA_DIR_NAMES for part in rel_parts)


def is_sensitive_linux_app_data(path: Path) -> bool:
    """Protect Linux user data that should not be removed as app cache/residue."""
    try:
        home = Path.home().resolve()
    except Exception:
        home = Path.home()

    # Only protect paths within the home directory
    if home not in path.parents and path != home:
        return False

    protected_paths = [home / rel for rel in LINUX_PROTECTED_HOME_PATHS]
    protected_paths.extend(home / ".var/app" / app_id for app_id in LINUX_PROTECTED_FLATPAK_APP_IDS)

    for protected in protected_paths:
        try:
            prot_path = protected.expanduser().resolve()
        except OSError:
            prot_path = protected.expanduser().absolute()
        if path == prot_path or prot_path in path.parents:
            return True
    return False
