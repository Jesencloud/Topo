import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .constants import DEFAULT_PURGE_SEARCH_PATHS
from .paths import get_config_dir


def get_config_file() -> Path:
    return get_config_dir() / "config.json"


DEFAULT_CONFIG = {
    "purge_search_paths": DEFAULT_PURGE_SEARCH_PATHS,
    "use_trash": True,
    "min_age_days": 7,
    "status_public_ip": False,
    "show_scrollbar": True,
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
        config = deepcopy(DEFAULT_CONFIG)
        save_config(config)
        return config

    try:
        with open(config_file) as f:
            user_config = json.load(f)
            return normalize_config(user_config)
    except (OSError, json.JSONDecodeError):
        return deepcopy(DEFAULT_CONFIG)


def normalize_config(user_config: Any) -> dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    if not isinstance(user_config, dict):
        return config

    purge_paths = user_config.get("purge_search_paths")
    if isinstance(purge_paths, list) and all(isinstance(p, str) for p in purge_paths):
        config["purge_search_paths"] = purge_paths

    min_age_days = user_config.get("min_age_days")
    if isinstance(min_age_days, int) and min_age_days >= 0:
        config["min_age_days"] = min_age_days

    for key in ("use_trash", "status_public_ip", "show_scrollbar"):
        value = user_config.get(key)
        if isinstance(value, bool):
            config[key] = value

    theme_color = user_config.get("theme_color")
    if isinstance(theme_color, str) and theme_color:
        config["theme_color"] = theme_color

    return config


def save_config(config: dict[str, Any]):
    _ensure_config()
    with open(get_config_file(), "w") as f:
        json.dump(config, f, indent=4)


def get_purge_paths() -> list[str]:
    config = load_config()
    return config.get("purge_search_paths", DEFAULT_CONFIG["purge_search_paths"])


def get_show_scrollbar() -> bool:
    config_file = get_config_file()
    if not config_file.exists():
        return DEFAULT_CONFIG["show_scrollbar"]
    return bool(load_config().get("show_scrollbar", DEFAULT_CONFIG["show_scrollbar"]))


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
