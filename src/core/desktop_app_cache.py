"""Shared desktop application cache definitions."""

from pathlib import Path

DESKTOP_APP_DEFS = {
    "Discord": {
        "cache_paths": (
            ".config/discord/Cache",
            ".config/discord/Code Cache",
            ".config/discord/GPUCache",
        ),
        "procs": ("discord",),
    },
    "Telegram": {
        "cache_paths": (
            ".local/share/TelegramDesktop/tdata/user_data/Cache",
            ".local/share/TelegramDesktop/tdata/user_data/temp",
        ),
        "procs": ("Telegram",),
    },
    "Slack": {
        "cache_paths": (
            ".config/Slack/Cache",
            ".config/Slack/Service Worker/CacheStorage",
        ),
        "procs": ("slack",),
    },
    "Spotify": {
        "cache_paths": (".cache/spotify/Data",),
        "procs": ("spotify",),
    },
    "Zoom": {
        "cache_paths": (".zoom/data",),
        "procs": ("zoom",),
    },
    "Microsoft Teams": {
        "cache_paths": (".config/Microsoft/Teams/Cache",),
        "procs": ("teams",),
    },
    "VLC": {
        "cache_paths": (".cache/vlc",),
        "procs": ("vlc",),
    },
    "OBS Studio": {
        "cache_paths": (".config/obs-studio/logs",),
        "procs": ("obs",),
    },
    "WeChat": {
        "cache_paths": (".var/app/com.tencent.WeChat/cache",),
        "extra_cleanup_paths": (
            ".var/app/com.tencent.WeChat/config/xwechat",
            ".xwechat",
            "Documents/WeChat Files",
        ),
        "procs": ("wechat", "wechat-uos", "wechat-universal", "WeChat.exe", "wechat.exe"),
    },
}


def _home_paths(paths: tuple[str, ...]) -> tuple[Path, ...]:
    home = Path.home()
    return tuple(home / path for path in paths)


def _resolve(path: str | Path) -> Path:
    raw_path = Path(path).expanduser()
    try:
        return raw_path.resolve(strict=False)
    except OSError:
        return raw_path.absolute()


def _cleanup_paths(info: dict) -> tuple[str, ...]:
    return (*info.get("cache_paths", ()), *info.get("extra_cleanup_paths", ()))


def _detection_name(path: str) -> str | None:
    parts = Path(path).parts
    if len(parts) >= 2 and parts[0] in (".cache", ".config"):
        return parts[1].lower()
    if parts and parts[0].startswith("."):
        return parts[0].lstrip(".").lower()
    return None


def get_desktop_app_cleanup_defs() -> dict[str, dict[str, tuple[Path, ...] | tuple[str, ...]]]:
    return {
        name: {
            "paths": _home_paths(_cleanup_paths(info)),
            "procs": info.get("procs", ()),
        }
        for name, info in DESKTOP_APP_DEFS.items()
    }


def iter_desktop_app_cache_paths() -> tuple[Path, ...]:
    return tuple(
        path
        for info in DESKTOP_APP_DEFS.values()
        for path in _home_paths(info.get("cache_paths", ()))
    )


def is_desktop_app_cache_path(path: str | Path) -> bool:
    resolved_path = _resolve(path)
    for cache_path in iter_desktop_app_cache_paths():
        resolved_cache_path = _resolve(cache_path)
        if resolved_path == resolved_cache_path or resolved_cache_path in resolved_path.parents:
            return True
    return False


DESKTOP_APP_DETECTION_NAMES = frozenset(
    name
    for info in DESKTOP_APP_DEFS.values()
    for path in _cleanup_paths(info)
    if (name := _detection_name(path))
)
