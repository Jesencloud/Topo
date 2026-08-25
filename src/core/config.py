import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .paths import get_config_dir


def get_config_file() -> Path:
    return get_config_dir() / "config.json"


# Bumped whenever a key starts being honoured, so load_config() can tell a
# deliberate user choice from a value that no released version ever read.
CONFIG_VERSION = 2

# Named title colors, resolved to escapes by constants._resolve_theme_title().
# Keeping the accepted set here (rather than accepting any string) means an
# unknown name is rejected at load time instead of blanking the title.
THEME_COLOR_NAMES = ("purple", "cyan", "blue", "magenta", "green", "yellow", "red")

DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "use_trash": True,
    # A floor, not a per-task threshold: every cleaner keeps its own window and
    # this can only push it further into the past. 0 means "no floor", i.e. the
    # shipped default changes nothing -- which matters because some sweeps
    # deliberately have no age gate at all (a container transfer cache, a snap's
    # ~/.cache), and a non-zero default would quietly start sparing files there.
    "min_age_days": 0,
    "show_scrollbar": True,
    # The color topo has always drawn its titles in. Changing this key is the
    # only thing that moves it.
    "theme_color": "purple",
}

# Keys that were written to config.json by <= 1.1.2 but read by nobody.
_LEGACY_INERT_KEYS = ("min_age_days", "theme_color")

_config_cache: dict[str, Any] | None = None


def load_config() -> dict[str, Any]:
    """Read config.json, or the defaults when it is absent or unreadable.

    Reading deliberately does not create the file. Every command now reads the
    config (the title color is resolved before the first line of output), and a
    read that wrote would mean `topo remove` created ~/.config/topo/config.json
    at startup and then reported it as leftover configuration it had removed.
    """
    config_file = get_config_file()
    if not config_file.exists():
        return deepcopy(DEFAULT_CONFIG)

    try:
        with open(config_file) as f:
            user_config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return deepcopy(DEFAULT_CONFIG)

    if isinstance(user_config, dict) and "config_version" not in user_config:
        # Written before these keys did anything. A stored value cannot be a
        # deliberate choice -- setting it changed nothing -- so it is dropped
        # rather than suddenly honoured: otherwise wiring the keys up would move
        # every existing install off the cleanup thresholds and the title color
        # it has always had. Stamped and rewritten once, so a choice made from
        # now on sticks.
        for key in _LEGACY_INERT_KEYS:
            user_config.pop(key, None)
        config = normalize_config(user_config)
        save_config(config)
        return config

    return normalize_config(user_config)


def normalize_config(user_config: Any) -> dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    if not isinstance(user_config, dict):
        return config

    min_age_days = user_config.get("min_age_days")
    if isinstance(min_age_days, int) and not isinstance(min_age_days, bool) and min_age_days >= 0:
        config["min_age_days"] = min_age_days

    for key in ("use_trash", "show_scrollbar"):
        value = user_config.get(key)
        if isinstance(value, bool):
            config[key] = value

    theme_color = user_config.get("theme_color")
    if isinstance(theme_color, str) and theme_color.lower() in THEME_COLOR_NAMES:
        config["theme_color"] = theme_color.lower()

    return config


def save_config(config: dict[str, Any]) -> bool:
    try:
        get_config_dir().mkdir(parents=True, exist_ok=True)
        with open(get_config_file(), "w") as f:
            json.dump(config, f, indent=4)
    except OSError:
        return False
    clear_config_cache()
    return True


def clear_config_cache() -> None:
    """Drop the memoized config so the next read picks the file up again."""
    global _config_cache
    _config_cache = None


def get_config() -> dict[str, Any]:
    """load_config() memoized for the life of the process.

    The deletion loops ask for ``use_trash`` and the age floor once per
    candidate -- tens of thousands of times in one `topo clean` -- and every
    load_config() call re-reads and re-parses the file. Nothing but save_config()
    changes it mid-run, and that drops the cache, so a hand edit between runs
    still takes effect on the next one.

    The result always carries every key and a validated value, because
    load_config() runs the file through normalize_config() -- which is why the
    getters below can index it and coerce, with no second round of checks.
    """
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def get_show_scrollbar() -> bool:
    return bool(get_config()["show_scrollbar"])


def get_use_trash() -> bool:
    """Whether a recoverable deletion goes to the trash instead of being wiped.

    Only consulted where the data is worth recovering -- app residue, backup
    files, the directories `topo analyze` deletes on request. Caches and stale
    temp files ignore it and are always deleted outright: moving a 4 GiB cache
    to ~/.local/share/Trash frees nothing, which is the one thing a cleanup tool
    must not pretend to have done.
    """
    return bool(get_config()["use_trash"])


def get_min_age_days() -> int:
    """The floor, in days, under which nothing is old enough to be cleaned.

    Cleaners keep their own thresholds (30 days for caches, 7 for editor
    backups, 3 for /tmp, and none at all for a couple of pure-cache sweeps);
    this raises any that sit below it. It cannot lower one, so no config edit can
    make a cleaner more aggressive than the code is. The default 0 leaves every
    threshold exactly where the code put it.
    """
    return int(get_config()["min_age_days"])


def get_theme_color() -> str:
    """Name of the title color; see THEME_COLOR_NAMES."""
    return str(get_config()["theme_color"])
