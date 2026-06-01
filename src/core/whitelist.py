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
    ".mozilla",
    ".thunderbird",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    ".config/vivaldi",
    ".config/opera",
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

LINUX_PROTECTED_FLATPAK_APP_IDS = [
    "app.zen_browser.zen",
    "com.bitwarden.desktop",
    "com.brave.Browser",
    "com.google.Chrome",
    "com.microsoft.Edge",
    "com.vivaldi.Vivaldi",
    "io.github.ungoogled_software.ungoogled_chromium",
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


def is_protected(path) -> bool:
    """Check if a path or its parent is in the whitelist."""
    if not isinstance(path, Path):
        path = Path(path)

    try:
        path = path.expanduser().resolve()
    except Exception:
        path = path.absolute()

    # 1. Exact matches for critical system paths
    path_str = str(path)
    if path_str == "/" or any(str(cp) == path_str for cp in DELETION_CRITICAL_EXACT_PATHS):
        return True

    # 2. Home directory exactly
    try:
        if path == Path.home().resolve():
            return True
    except Exception:
        pass

    # 3. Critical prefixes (recursive protection)
    for prefix in CRITICAL_PREFIX_PATHS:
        try:
            prefix_res = prefix.resolve()
        except Exception:
            prefix_res = prefix.absolute()

        if path == prefix_res or prefix_res in path.parents:
            # Carve-out: allow cleaning /var/tmp and /var/cache contents
            is_carve_out = False
            for allow in ["/var/tmp/", "/var/cache/"]:
                if path_str.startswith(allow):
                    is_carve_out = True
                    break
            if not is_carve_out:
                return True

    # 4. Sensitive app data (in home)
    if is_sensitive_linux_app_data(path):
        return True

    # 5. Topo self-protection
    try:
        topo_config = get_config_dir().resolve()
        if path == topo_config or topo_config in path.parents:
            return True
    except Exception:
        pass

    # 6. User whitelist (absolute recursive protection)
    whitelist = get_whitelist()
    for prot_str in whitelist:
        try:
            prot_path = Path(prot_str).expanduser().resolve()
            if path == prot_path or prot_path in path.parents:
                return True
        except Exception:
            continue

    return False


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
