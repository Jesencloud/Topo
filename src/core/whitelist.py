import json
from pathlib import Path

from .paths import get_config_dir


def get_whitelist_file() -> Path:
    return get_config_dir() / "whitelist.json"


DEFAULT_CRITICAL_PATHS = [
    "/",
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
CRITICAL_PREFIX_PATHS = tuple(
    Path(path) for path in DEFAULT_CRITICAL_PATHS if path != "/"
)
DELETION_CRITICAL_EXACT_PATHS = tuple(Path(path) for path in ("/", "/home", "/mnt", "/media", "/srv"))

LINUX_PROTECTED_HOME_PATHS = [
    # Credentials and encryption material
    ".ssh",
    ".gnupg",
    ".pki",
    ".password-store",
    ".local/share/keyrings",
    # Browser profiles
    ".mozilla",
    ".config/google-chrome",
    ".config/chromium",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    ".config/vivaldi",
    ".config/opera",
    # Password managers and authenticators
    ".config/Bitwarden",
    ".config/1Password",
    ".config/keepassxc",
    ".config/KeePassXC",
    ".local/share/keepassxc",
    ".local/share/KeePassXC",
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
    # Desktop environment settings
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
    ".local/share/gnome-shell",
    ".local/share/gvfs-metadata",
    ".local/share/nautilus",
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
]


def _ensure_config():
    config_dir = get_config_dir()
    whitelist_file = get_whitelist_file()
    if not config_dir.exists():
        config_dir.mkdir(parents=True, exist_ok=True)
    if not whitelist_file.exists():
        with open(whitelist_file, "w") as f:
            # Seed with default critical system paths
            json.dump(DEFAULT_CRITICAL_PATHS, f, indent=4)


def get_whitelist():
    _ensure_config()
    try:
        with open(get_whitelist_file()) as f:
            return json.load(f)
    except Exception:
        return []


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

    # 1. Always protect root exactly
    if str(path) == "/":
        return True

    # 2. Check hardcoded critical prefixes (protects them and their children)
    # We exclude "/" here because it's handled above and shouldn't be recursive
    for cp_path in CRITICAL_PREFIX_PATHS:
        if path == cp_path or cp_path in path.parents:
            return True

    if is_sensitive_linux_app_data(path):
        return True

    # 3. Check User Whitelist
    whitelist = get_whitelist()
    for protected in whitelist:
        try:
            prot_path = Path(protected).expanduser().resolve()
            if path == prot_path:
                return True
            # Protect children, but skip root to avoid protecting everything
            if str(prot_path) != "/" and prot_path in path.parents:
                return True
        except Exception:
            continue
    return False


def is_sensitive_linux_app_data(path: Path) -> bool:
    """Protect Linux user data that should not be removed as app cache/residue."""
    home = Path.home()
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
