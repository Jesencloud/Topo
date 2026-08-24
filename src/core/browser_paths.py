"""Where browsers keep their data: one table, three consumers with opposite jobs.

Protection (``core.whitelist``) reads it to refuse to delete a profile, cleanup
(``clean.apps``) to sweep the caches beside it, and database optimization
(``optimize``) to find the SQLite files inside it. A fact used by policies that
disagree cannot live inside any one of them, which is why this sits in ``core``.

Every path is home-relative, and every install format is listed side by side:
one machine can hold the native, Flatpak and Snap builds of the same browser.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import TypedDict


class BrowserDef(TypedDict, total=False):
    """One browser's locations, plus the two facts derived paths need.

    ``profile_glob`` is the glob from a profile root down to the individual
    profile directories: "*" for the browsers that put them straight into the
    root, deeper for the two that nest them, and empty for the one whose root
    *is* its only profile. ``engine`` names the database family, which is what
    tells a consumer the file names inside a profile.
    """

    profile_roots: tuple[str, ...]
    cache_roots: tuple[str, ...]
    flatpak_ids: tuple[str, ...]
    procs: tuple[str, ...]
    engine: str
    profile_glob: str


def _xdg_browser_roots(*names: str) -> BrowserDef:
    return {
        "profile_roots": tuple(f".config/{name}" for name in names),
        "cache_roots": tuple(f".cache/{name}" for name in names),
    }


def _home_browser_roots(name: str) -> BrowserDef:
    return {
        "profile_roots": (f".{name}",),
        "cache_roots": (f".cache/{name}",),
    }


def _snap_root(snap_name: str, relative: str) -> str:
    """A home-relative path for a snap-confined browser's data.

    Snap confinement moves everything under ~/snap/<name>/common, so the native
    entries below match nothing for a snap install. Ubuntu ships both Firefox and
    Chromium as snaps by default, which left the most common Debian-family
    desktop with unprotected browser profiles and unreachable browser caches.
    """
    return f"snap/{snap_name}/common/{relative}"


def _flatpak_root(app_id: str, home_relative: str) -> str:
    """Where a Flatpak install relocates a home-relative path.

    Flatpak points XDG_CONFIG_HOME at ~/.var/app/<id>/config and HOME itself at
    ~/.var/app/<id>, so ".config/chromium" moves to
    ".var/app/<id>/config/chromium" while ".mozilla" moves to
    ".var/app/<id>/.mozilla". Both shapes occur, which is exactly why deriving
    them beats writing one literal per browser per install format by hand.
    """
    xdg_config = ".config/"
    if home_relative.startswith(xdg_config):
        return f".var/app/{app_id}/config/{home_relative[len(xdg_config) :]}"
    return f".var/app/{app_id}/{home_relative}"


BROWSER_DEFS: dict[str, BrowserDef] = {
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
        "engine": "chromium",
    },
    "Chromium": {
        "profile_roots": (
            ".config/chromium",
            ".config/ungoogled-chromium",
            # The chromium snap puts its user-data-dir straight into
            # $SNAP_USER_COMMON instead of mirroring the native .config layout.
            _snap_root("chromium", "chromium"),
        ),
        "cache_roots": (
            ".cache/chromium",
            ".cache/ungoogled-chromium",
            _snap_root("chromium", ".cache/chromium"),
        ),
        "flatpak_ids": (
            "org.chromium.Chromium",
            "io.github.ungoogled_software.ungoogled_chromium",
            "com.github.Eloston.UngoogledChromium",
        ),
        "procs": ("chromium", "chromium-browser", "ungoogled-chromium"),
        "engine": "chromium",
    },
    "Brave Browser": {
        **_xdg_browser_roots("BraveSoftware"),
        "flatpak_ids": ("com.brave.Browser",),
        "procs": ("brave", "brave-browser", "brave-browser-stable"),
        "engine": "chromium",
        # Brave is the one Chromium build that does not use its config directory
        # as the user-data-dir: ~/.config/BraveSoftware holds Brave-Browser,
        # and the profiles are a level below that.
        "profile_glob": "Brave-Browser/*",
    },
    "Microsoft Edge": {
        **_xdg_browser_roots("microsoft-edge", "microsoft-edge-beta", "microsoft-edge-dev"),
        "flatpak_ids": ("com.microsoft.Edge", "com.microsoft.EdgeDev"),
        "procs": ("microsoft-edge", "microsoft-edge-beta", "microsoft-edge-dev", "msedge"),
        "engine": "chromium",
    },
    "Vivaldi": {
        **_xdg_browser_roots("vivaldi", "vivaldi-snapshot"),
        "flatpak_ids": ("com.vivaldi.Vivaldi",),
        "procs": ("vivaldi", "vivaldi-bin", "vivaldi-snapshot"),
        "engine": "chromium",
    },
    "Opera": {
        **_xdg_browser_roots("opera", "opera-beta", "opera-developer"),
        "flatpak_ids": ("com.opera.Opera",),
        "procs": ("opera", "opera-beta", "opera-developer"),
        "engine": "chromium",
        # Alone among the Chromium builds, Opera uses its config directory as the
        # profile rather than as a container of profiles: opera://about reports
        # ~/.config/opera itself, and History sits directly in it. A "*" level
        # here would look one directory too deep and find nothing.
        "profile_glob": "",
    },
    "Firefox": {
        # The snap keeps the familiar .mozilla layout, just relocated.
        "profile_roots": (".mozilla", ".config/mozilla", _snap_root("firefox", ".mozilla")),
        "cache_roots": (".cache/mozilla", _snap_root("firefox", ".cache/mozilla")),
        "flatpak_ids": ("org.mozilla.firefox",),
        "procs": ("firefox", "firefox-bin", "firefox-esr"),
        "engine": "gecko",
        # Every Gecko browser but Firefox keeps its profiles directly under the
        # root; Firefox puts them one level down, beside profiles.ini.
        "profile_glob": "firefox/*",
    },
    "LibreWolf": {
        **_home_browser_roots("librewolf"),
        "flatpak_ids": ("io.gitlab.librewolf-community",),
        "procs": ("librewolf",),
        "engine": "gecko",
    },
    "Floorp": {
        **_home_browser_roots("floorp"),
        "procs": ("floorp",),
        "engine": "gecko",
    },
    "Waterfox": {
        **_home_browser_roots("waterfox"),
        "procs": ("waterfox",),
        "engine": "gecko",
    },
    "Zen Browser": {
        **_home_browser_roots("zen"),
        "flatpak_ids": ("app.zen_browser.zen",),
        "procs": ("zen", "zen-bin", "zen-browser"),
        "engine": "gecko",
    },
    "Thorium": {
        **_xdg_browser_roots("thorium", "Thorium"),
        "procs": ("thorium", "thorium-browser"),
        "engine": "chromium",
    },
    "Yandex Browser": {
        **_xdg_browser_roots("yandex-browser", "yandex-browser-beta"),
        "procs": ("yandex-browser", "yandex-browser-beta"),
        "engine": "chromium",
    },
}


def _browser_cleanup_roots(info: BrowserDef) -> tuple[str, ...]:
    profile_roots = info.get("profile_roots", ())
    cache_roots = info.get("cache_roots", ())
    flatpak_ids = info.get("flatpak_ids", ())
    flatpak_roots = tuple(f".var/app/{app_id}" for app_id in flatpak_ids)
    return (*profile_roots, *cache_roots, *flatpak_roots)


BROWSER_PROFILE_PATHS = tuple(
    root for info in BROWSER_DEFS.values() for root in info.get("profile_roots", ())
)
BROWSER_FLATPAK_APP_IDS = tuple(
    app_id for info in BROWSER_DEFS.values() for app_id in info.get("flatpak_ids", ())
)

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


@dataclass(frozen=True)
class BrowserProfileTarget:
    """Where one browser's individual profile directories are, per install format.

    ``profile_globs`` are home-relative and each already ends at a single profile
    directory, so a consumer appends only the file it wants -- no consumer needs
    to know that Firefox nests its profiles under "firefox/" or that Flatpak
    moves the whole tree.
    """

    name: str
    engine: str
    procs: tuple[str, ...]
    profile_globs: tuple[str, ...]


def _browser_profile_globs(info: BrowserDef) -> tuple[str, ...]:
    roots = info.get("profile_roots", ())
    # Flatpak relocates the *native* roots, so only those are crossed with the
    # ids: pointing a Flatpak prefix at a root snap already moved would name a
    # directory no install can have. Within the native roots the cross is total,
    # because guessing which config directory each id ships would be worse than
    # a few combinations that simply match nothing.
    native_roots = tuple(root for root in roots if not root.startswith("snap/"))
    flatpak_roots = tuple(
        _flatpak_root(app_id, root)
        for app_id in info.get("flatpak_ids", ())
        for root in native_roots
    )
    profile_glob = info.get("profile_glob", "*")
    return tuple(
        f"{root}/{profile_glob}" if profile_glob else root for root in (*roots, *flatpak_roots)
    )


BROWSER_PROFILE_TARGETS = tuple(
    BrowserProfileTarget(
        name=name,
        # Not .get(): a browser that forgot to declare its engine would be
        # silently invisible to database optimization, so the table is required
        # to be complete and tests/test_browser_paths.py checks that it is.
        engine=info["engine"],
        procs=info.get("procs", ()),
        profile_globs=_browser_profile_globs(info),
    )
    for name, info in BROWSER_DEFS.items()
)
