import json
from pathlib import Path
from typing import Any


def get_config_dir() -> Path:
    return Path.home() / ".config" / "topo"


def get_config_file() -> Path:
    return get_config_dir() / "config.json"


DEFAULT_CONFIG = {
    "purge_search_paths": [
        str(Path.home() / "Documents"),
        str(Path.home() / "Projects"),
        str(Path.home() / "Code"),
        str(Path.home() / "Development"),
        str(Path.home() / "src"),
        str(Path.home() / "repos"),
        str(Path.home() / "workspace"),
    ],
    "use_trash": True,
    "min_age_days": 7,
    "theme_color": "cyan",
}


def _ensure_config():
    config_dir = get_config_dir()
    if not config_dir.exists():
        config_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> dict[str, Any]:
    _ensure_config()
    config_file = get_config_file()
    if not config_file.exists():
        save_config(DEFAULT_CONFIG)
        return DEFAULT_CONFIG

    try:
        with open(config_file) as f:
            user_config = json.load(f)
            # Merge with defaults to ensure all keys exist
            config = DEFAULT_CONFIG.copy()
            config.update(user_config)
            return config
    except Exception:
        return DEFAULT_CONFIG


def save_config(config: dict[str, Any]):
    _ensure_config()
    with open(get_config_file(), "w") as f:
        json.dump(config, f, indent=4)


def get_purge_paths() -> list[str]:
    config = load_config()
    return config.get("purge_search_paths", DEFAULT_CONFIG["purge_search_paths"])


def add_purge_path(path_str: str) -> bool:
    path = str(Path(path_str).expanduser().resolve())
    config = load_config()
    paths = config.get("purge_search_paths", [])
    if path not in paths:
        paths.append(path)
        config["purge_search_paths"] = paths
        save_config(config)
        return True
    return False


def remove_purge_path(path_str: str) -> bool:
    path = str(Path(path_str).expanduser().resolve())
    config = load_config()
    paths = config.get("purge_search_paths", [])
    if path in paths:
        paths.remove(path)
        config["purge_search_paths"] = paths
        save_config(config)
        return True
    return False
