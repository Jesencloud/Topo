import json
from pathlib import Path


def get_config_dir() -> Path:
    return Path.home() / ".config" / "topo"


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
    "/sbin",
    "/sys",
    "/usr",
    "/var",
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
    critical_prefixes = [
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/lib",
        "/lib64",
        "/proc",
        "/root",
        "/sbin",
        "/sys",
        "/usr",
        "/var",
    ]
    for cp in critical_prefixes:
        cp_path = Path(cp)
        if path == cp_path or cp_path in path.parents:
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
