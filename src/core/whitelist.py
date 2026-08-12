import functools
import json
import os
from pathlib import Path

from .browser_cache import (
    BROWSER_FLATPAK_APP_IDS,
    BROWSER_PROFILE_PATHS,
    CLEANABLE_APP_CACHE_DIR_NAMES,
)
from .install_source import get_install_root
from .paths import get_config_dir

PATH_RESOLVE_ERRORS = (OSError, RuntimeError)


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

# Paths that are only protected from exact deletion.
DELETION_CRITICAL_EXACT_PATHS = (
    Path("/"),
    Path("/") / "home",
    Path("/") / "mnt",
    Path("/") / "media",
    Path("/") / "srv",
    Path("/") / "usr",
    Path("/") / "var",
    Path("/") / "tmp",
    Path("/") / "boot",
)

# The /var carve-out: which content inside otherwise-protected system roots Topo
# is allowed to remove. Kept here as the single source of truth so file_ops and
# the whitelist can never drift apart on the same question.
SYSTEM_TEMP_ROOT = Path("/var/tmp")  # nosec B108 - cleanup root, not a temp file
SYSTEM_CLEANABLE_ROOTS = (
    SYSTEM_TEMP_ROOT,
    Path("/var/cache"),
)
# Package-manager and regenerable-index caches only. Anything else under
# /var/cache (ldconfig, private, unattended-upgrades state, …) stays protected.
SYSTEM_CLEANABLE_ALLOWLIST = frozenset(
    {
        "/var/cache/apk",
        "/var/cache/apt/archives",
        "/var/cache/dnf",
        "/var/cache/dnf5daemon-server",
        "/var/cache/fontconfig",
        "/var/cache/libdnf5",
        "/var/cache/man",
        "/var/cache/pacman/pkg",
        "/var/cache/PackageKit",
        "/var/cache/yum",
        "/var/cache/zypp",
    }
)

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
    *BROWSER_PROFILE_PATHS,
    ".thunderbird",
    # Messaging and social
    ".local/share/TelegramDesktop",
    ".config/QQ",
    ".config/tencent-qq",
    ".config/Tencent",
    ".config/tencent",
    ".config/wechat",
    ".config/WeChat",
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
    # Password managers & 2FA authenticators
    ".config/Bitwarden",
    ".config/1Password",
    ".config/keepassxc",
    ".config/KeePassXC",
    ".local/share/keepassxc",
    ".local/share/KeePassXC",
    ".config/authy-desktop",
    # Crypto wallets (non-recoverable private keys)
    ".electrum",
    ".config/Electrum",
    ".config/Exodus",
    ".config/Ledger Live",
    ".config/Trezor",
    # Cloud & Remote credentials
    ".config/gcloud",
    ".config/rclone",
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
    *BROWSER_FLATPAK_APP_IDS,
    "com.bitwarden.desktop",
    "md.obsidian.Obsidian",
    "org.gnome.World.Secrets",
    "org.keepassxc.KeePassXC",
    "org.mozilla.Thunderbird",
    "org.pgadmin.pgadmin4",
    "org.telegram.desktop",
    "com.discordapp.Discord",
    "com.slack.Slack",
    "im.riot.Riot",
]


def _get_resolved_home() -> Path:
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return Path.home()


def _ensure_config():
    try:
        config_dir = get_config_dir()
        whitelist_file = get_whitelist_file()
        if not config_dir.exists():
            config_dir.mkdir(parents=True, exist_ok=True)
        if not whitelist_file.exists():
            with open(whitelist_file, "w") as f:
                # Seed with empty list; critical paths are hardcoded for safety
                json.dump([], f, indent=4)
    except OSError:
        pass


def get_whitelist():
    _ensure_config()
    try:
        with open(get_whitelist_file()) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [path for path in data if path not in LEGACY_SEEDED_WHITELIST_PATHS]


def add_to_whitelist(path_str: str) -> bool:
    _ensure_config()
    path = Path(path_str).expanduser().resolve()
    current = get_whitelist()
    if str(path) not in current:
        current.append(str(path))
        try:
            with open(get_whitelist_file(), "w") as f:
                json.dump(current, f, indent=4)
            get_hard_protection_reason_cached.cache_clear()
            return True
        except OSError:
            return False
    return False


def remove_from_whitelist(path_str: str) -> bool:
    _ensure_config()
    path = Path(path_str).expanduser().resolve()
    current = get_whitelist()
    if str(path) in current:
        current.remove(str(path))
        try:
            with open(get_whitelist_file(), "w") as f:
                json.dump(current, f, indent=4)
            get_hard_protection_reason_cached.cache_clear()
            return True
        except OSError:
            return False
    return False


@functools.lru_cache(maxsize=4096)
def _resolve_path_str(path_str: str) -> Path:
    p = Path(path_str)
    try:
        return p.expanduser().resolve()
    except PATH_RESOLVE_ERRORS:
        return p.absolute()


def _resolve_path(path) -> Path:
    return _resolve_path_str(str(path))


def is_system_cleanable_content(path: Path) -> bool:
    """Return True when *path* is content inside a system cache/temp root that
    Topo may remove — the single source of truth for the /var carve-out.

    The carve-out used to be a whole-subtree prefix match, which handed root
    everything under /var/cache (including non-package state such as
    /var/cache/ldconfig or /var/cache/private) and every other user's private
    data under /var/tmp. It is now split by what actually makes a path safe:

    * /var/cache — membership in an explicit package-manager cache allowlist.
    * /var/tmp   — ownership by the current user, matching the same rule
      clean_system_temp() already applies to its entries.

    The roots themselves are never cleanable, only their contents.
    """
    if not any(root in path.parents for root in SYSTEM_CLEANABLE_ROOTS):
        return False

    for entry in SYSTEM_CLEANABLE_ALLOWLIST:
        entry_path = Path(entry)
        if path == entry_path or entry_path in path.parents:
            return True

    if SYSTEM_TEMP_ROOT not in path.parents:
        return False
    try:
        return path.lstat().st_uid == os.getuid()
    except FileNotFoundError:
        # Nothing there to protect, and the deletion path reports the absence
        # itself; refusing here would only mislabel the reason.
        return True
    except OSError:
        return False


def _is_critical_system_path(path: Path) -> bool:
    if path == Path("/") or path in DELETION_CRITICAL_EXACT_PATHS:
        return True

    for prefix in CRITICAL_PREFIX_PATHS:
        try:
            prefix_res = prefix.resolve()
        except PATH_RESOLVE_ERRORS:
            prefix_res = prefix.absolute()

        if (path == prefix_res or prefix_res in path.parents) and not is_system_cleanable_content(
            path
        ):
            return True
    return False


@functools.lru_cache(maxsize=4096)
def get_hard_protection_reason_cached(path_str: str) -> str | None:
    path = _resolve_path(path_str)

    if _is_critical_system_path(path):
        return "critical system path"

    home = _get_resolved_home()
    if path == home:
        return "home directory"

    # Protect standard XDG user-data directories themselves (exact match) from
    # every deletion context, including uninstall residue removal. Files *inside*
    # them remain deletable via Analyze, so this only blocks wiping the whole dir.
    for rel in LINUX_USER_DATA_DIRS:
        try:
            user_dir = (home / rel).resolve()
        except PATH_RESOLVE_ERRORS:
            user_dir = (home / rel).absolute()
        if path == user_dir:
            return "user data directory"

    protected_home_paths = [home / rel for rel in LINUX_HARD_PROTECTED_HOME_PATHS]
    for protected in protected_home_paths:
        try:
            prot_path = protected.expanduser().resolve()
        except PATH_RESOLVE_ERRORS:
            prot_path = protected.expanduser().absolute()
        if path == prot_path or prot_path in path.parents:
            return "credential or identity data"

    try:
        topo_config = get_config_dir().resolve()
        if path == topo_config or topo_config in path.parents:
            return "Topo configuration"

        topo_root = get_install_root().resolve()
        if path == topo_root or topo_root in path.parents:
            return "Topo installation"
    except PATH_RESOLVE_ERRORS:
        pass

    for prot_str in get_whitelist():
        try:
            prot_path = Path(prot_str).expanduser().resolve()
            if path == prot_path or prot_path in path.parents:
                return "user whitelist"
        except PATH_RESOLVE_ERRORS:
            continue
    return None


def get_hard_protection_reason(path) -> str | None:
    """Return why a path is protected across every deletion context."""
    return get_hard_protection_reason_cached(str(path))


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

    home = _get_resolved_home()
    try:
        rel_parts = path.relative_to(home).parts
    except ValueError:
        return False

    return any(part in CLEANABLE_APP_CACHE_DIR_NAMES for part in rel_parts)


def is_sensitive_linux_app_data(path: Path) -> bool:
    """Protect Linux user data that should not be removed as app cache/residue."""
    home = _get_resolved_home()

    # Only protect paths within the home directory
    if home not in path.parents and path != home:
        return False

    protected_paths = [home / rel for rel in LINUX_PROTECTED_HOME_PATHS]
    protected_paths.extend(home / ".var/app" / app_id for app_id in LINUX_PROTECTED_FLATPAK_APP_IDS)

    for protected in protected_paths:
        try:
            prot_path = protected.expanduser().resolve()
        except PATH_RESOLVE_ERRORS:
            prot_path = protected.expanduser().absolute()
        if path == prot_path or prot_path in path.parents:
            return True
    return False
