import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import get_config_dir


def get_config_file() -> Path:
    return get_config_dir() / "config.json"


DEFAULT_CONFIG = {
    "use_trash": True,
    "min_age_days": 7,
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

    min_age_days = user_config.get("min_age_days")
    if isinstance(min_age_days, int) and min_age_days >= 0:
        config["min_age_days"] = min_age_days

    for key in ("use_trash", "show_scrollbar"):
        value = user_config.get(key)
        if isinstance(value, bool):
            config[key] = value

    theme_color = user_config.get("theme_color")
    if isinstance(theme_color, str) and theme_color:
        config["theme_color"] = theme_color

    return config


def save_config(config: dict[str, Any]) -> bool:
    try:
        _ensure_config()
        with open(get_config_file(), "w") as f:
            json.dump(config, f, indent=4)
        return True
    except OSError:
        return False


def get_show_scrollbar() -> bool:
    return bool(load_config().get("show_scrollbar", DEFAULT_CONFIG["show_scrollbar"]))
