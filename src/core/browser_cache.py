"""Shared browser cache/profile definitions used by cleanup and protection code."""

from pathlib import Path


def _xdg_browser_roots(*names: str) -> dict[str, tuple[str, ...]]:
    return {
        "profile_roots": tuple(f".config/{name}" for name in names),
        "cache_roots": tuple(f".cache/{name}" for name in names),
    }


def _home_browser_roots(name: str) -> dict[str, tuple[str, ...]]:
    return {
        "profile_roots": (f".{name}",),
        "cache_roots": (f".cache/{name}",),
    }


BROWSER_DEFS = {
    "Google Chrome": {
        **_xdg_browser_roots("google-chrome", "google-chrome-beta", "google-chrome-unstable"),
        "flatpak_ids": ("com.google.Chrome", "com.google.ChromeDev"),
        "procs": (
            "chrome",
            "google-chrome",
            "google-chrome-beta",
            "google-chrome-stable",
            "google-chrome-unstable",
        ),
    },
    "Chromium": {
        **_xdg_browser_roots("chromium", "ungoogled-chromium"),
        "flatpak_ids": (
            "org.chromium.Chromium",
            "io.github.ungoogled_software.ungoogled_chromium",
            "com.github.Eloston.UngoogledChromium",
        ),
        "procs": ("chromium", "chromium-browser", "ungoogled-chromium"),
    },
    "Brave Browser": {
        **_xdg_browser_roots("BraveSoftware"),
        "flatpak_ids": ("com.brave.Browser",),
        "procs": ("brave", "brave-browser", "brave-browser-stable"),
    },
    "Microsoft Edge": {
        **_xdg_browser_roots("microsoft-edge", "microsoft-edge-beta", "microsoft-edge-dev"),
        "flatpak_ids": ("com.microsoft.Edge", "com.microsoft.EdgeDev"),
        "procs": ("microsoft-edge", "microsoft-edge-beta", "microsoft-edge-dev", "msedge"),
    },
    "Vivaldi": {
        **_xdg_browser_roots("vivaldi", "vivaldi-snapshot"),
        "flatpak_ids": ("com.vivaldi.Vivaldi",),
        "procs": ("vivaldi", "vivaldi-bin", "vivaldi-snapshot"),
    },
    "Opera": {
        **_xdg_browser_roots("opera", "opera-beta", "opera-developer"),
        "flatpak_ids": ("com.opera.Opera",),
        "procs": ("opera", "opera-beta", "opera-developer"),
    },
    "Firefox": {
        **_home_browser_roots("mozilla"),
        "flatpak_ids": ("org.mozilla.firefox",),
        "procs": ("firefox", "firefox-esr"),
    },
    "LibreWolf": {
        **_home_browser_roots("librewolf"),
        "flatpak_ids": ("io.gitlab.librewolf-community",),
        "procs": ("librewolf",),
    },
    "Floorp": {
        **_home_browser_roots("floorp"),
        "procs": ("floorp",),
    },
    "Waterfox": {
        **_home_browser_roots("waterfox"),
        "procs": ("waterfox",),
    },
    "Zen Browser": {
        **_home_browser_roots("zen"),
        "flatpak_ids": ("app.zen_browser.zen",),
        "procs": ("zen", "zen-bin", "zen-browser"),
    },
    "Thorium": {
        **_xdg_browser_roots("thorium", "Thorium"),
        "procs": ("thorium", "thorium-browser"),
    },
    "Yandex Browser": {
        **_xdg_browser_roots("yandex-browser", "yandex-browser-beta"),
        "procs": ("yandex-browser", "yandex-browser-beta"),
    },
}


def _flatten_browser_values(key: str) -> tuple[str, ...]:
    return tuple(value for info in BROWSER_DEFS.values() for value in info.get(key, ()))


def _browser_cleanup_roots(info: dict) -> tuple[str, ...]:
    profile_roots = info.get("profile_roots", ())
    cache_roots = info.get("cache_roots", ())
    flatpak_roots = tuple(f".var/app/{app_id}" for app_id in info.get("flatpak_ids", ()))
    return (*profile_roots, *cache_roots, *flatpak_roots)


BROWSER_PROFILE_PATHS = _flatten_browser_values("profile_roots")
BROWSER_FLATPAK_APP_IDS = _flatten_browser_values("flatpak_ids")

CLEANABLE_APP_CACHE_DIR_NAMES = frozenset(
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
        "ShaderCache",
        "startupCache",
    }
)

BROWSER_CACHE_DEFS = {
    name: {
        "roots": _browser_cleanup_roots(info),
        "procs": info.get("procs", ()),
    }
    for name, info in BROWSER_DEFS.items()
}

BROWSER_CACHE_ROOT_NAMES = frozenset(
    Path(root).name.lower()
    for info in BROWSER_CACHE_DEFS.values()
    for root in info.get("roots", ())
)
