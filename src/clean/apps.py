import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..core.app_cache import (
    find_cleanable_cache_dirs_in_roots,
    find_standard_cache_dirs,
    find_xdg_cache_candidates,
    resolve_cache_path,
)
from ..core.browser_paths import BROWSER_CACHE_DEFS, BROWSER_CACHE_ROOT_NAMES
from ..core.config import get_use_trash
from ..core.constants import (
    CLEAN_CACHE_AGE_DAYS,
    DETECTED_APPS_FILE,
    GRAY,
    INFO,
    OK,
    RESET,
    SKIP,
)
from ..core.desktop_app_cache import (
    DESKTOP_APP_DETECTION_NAMES,
    get_desktop_app_cleanup_defs,
)
from ..core.desktop_entry import get_desktop_exec_command
from ..core.file_ops import (
    CLEANED_PATHS,
    age_cutoff,
    bytes_to_human,
    clean_path_by_age,
    get_direct_child_sizes_fast,
    get_size_fast,
    is_app_running,
    parse_size_from_text,
    register_cleaned_path,
    safe_remove,
)
from ..core.json_store import read_json, write_json_atomic
from ..core.system import C_LOCALE_ENV, PACKAGE_TRANSACTION_TIMEOUT, run_command
from ..core.text import sanitize_for_display


def proactive_app_detection():
    """Scans for installed apps and matches them with their folders. Also prunes dead entries."""
    detected = {}
    # Unlike the whitelist, this file is derived data: everything in it was found
    # by the scan below and will be found again, so a file we cannot read is
    # rebuilt rather than protected. What it must not do is crash -- json.load()
    # on a hand-edited file whose bytes are not UTF-8 raises UnicodeDecodeError,
    # a ValueError that `except (OSError, JSONDecodeError)` does not catch -- or
    # be trusted to hold a dict just because it parsed.
    stored, state = read_json(DETECTED_APPS_FILE)
    if isinstance(stored, dict):
        # One level deep, because a value that is not a dict parses fine and then
        # raises AttributeError on info.get("paths") in the prune below.
        detected = {name: info for name, info in stored.items() if isinstance(info, dict)}
    unusable_registry = state != "missing" and (
        not isinstance(stored, dict) or len(detected) != len(stored)
    )

    # 1. Health Check: Prune entries that no longer have a binary AND no longer have data
    original_count = len(detected)
    to_delete = [
        name
        for name, info in detected.items()
        if not (shutil.which(name) or shutil.which(name.lower()))
        and not any(Path(p).expanduser().exists() for p in info.get("paths", []))
    ]
    for name in to_delete:
        del detected[name]

    # 2. Discovery: Find new apps
    handled_names = {n.lower() for n in get_desktop_app_cleanup_defs()}
    handled_names.update(DESKTOP_APP_DETECTION_NAMES)
    handled_names.update(n.lower() for n in BROWSER_CACHE_DEFS)
    handled_names.update(BROWSER_CACHE_ROOT_NAMES)
    handled_names.update(n.lower() for n in detected)

    new_found = False
    for root_str in ["~/.cache"]:
        root = Path(root_str).expanduser()
        if not root.exists():
            continue
        try:
            for item in root.iterdir():
                # Skip symlinks: resolving one would pull its (possibly
                # out-of-tree) target into the cleanup set, so a ~/.cache/<cmd>
                # link pointing at real data could later have its contents wiped.
                # Only manage real directories that physically live here.
                if item.is_symlink() or not item.is_dir() or item.name.startswith("."):
                    continue

                # SELF-PROTECTION: Never detect Topo's own configuration directory
                if item.resolve() == DETECTED_APPS_FILE.parent.resolve():
                    continue

                name_lower = item.name.lower()
                if name_lower in handled_names:
                    continue

                if shutil.which(name_lower) or shutil.which(item.name):
                    detected[item.name] = {"paths": [str(item.resolve())], "procs": [name_lower]}
                    handled_names.add(name_lower)
                    new_found = True
        except OSError:
            pass

    # Save if we found NEW things OR if we PRUNED old things -- or if what is
    # there cannot be read, in which case leaving it alone would mean re-reading
    # the same unusable file on every run.
    if (
        new_found
        or len(detected) != original_count
        or unusable_registry
        or not DETECTED_APPS_FILE.exists()
    ):
        try:
            DETECTED_APPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return detected
        if write_json_atomic(DETECTED_APPS_FILE, detected) and new_found:
            msg = f"  {INFO} {GRAY}Updated local app registry ({len(detected)} apps known){RESET}"
            print(msg)
    return detected


def clean_app_generic(name, paths, process_names=None, dry_run=False):
    """Unified cleaner for any app with strict process safety."""
    if process_names and any(is_app_running(p) for p in process_names):
        display_name = name.removesuffix(" Cache")
        print(f"  {SKIP} {display_name} is active · cache cleanup skipped")
        return 0, 0

    total_freed = 0
    items_cleaned = 0
    found = False
    for p_str in paths:
        raw_path = Path(p_str).expanduser()
        if raw_path.is_symlink():
            continue
        path = raw_path.resolve()
        register_cleaned_path(path)
        if path.exists():
            found = True
            if dry_run:
                size = get_size_fast(path)
                safe_remove(path, use_trash=False, dry_run=True, known_size_bytes=size)
                total_freed += size
                items_cleaned += 1
                continue
            try:
                if path.is_dir():
                    child_sizes = get_direct_child_sizes_fast(path)
                    for item in path.iterdir():
                        s = (
                            get_size_fast(item)
                            if child_sizes is None
                            else child_sizes.get(item.name, 0)
                        )
                        if safe_remove(item, use_trash=False, known_size_bytes=s)[0]:
                            total_freed += s
                            items_cleaned += 1
                else:
                    size = get_size_fast(path)
                    if safe_remove(path, use_trash=False, known_size_bytes=size)[0]:
                        total_freed += size
                        items_cleaned += 1
            except OSError:
                continue

    if found and (total_freed > 0 or dry_run):
        glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cache cleaned")
        print(f"  {glyph} {name} ({bytes_to_human(total_freed)}) {status}")
        return total_freed, items_cleaned
    return 0, 0


def clean_browser_caches(dry_run=False):
    """Clean cache directories for known browser profile layouts."""
    total_size = 0
    total_items = 0
    total_categories = 0
    for name, info in BROWSER_CACHE_DEFS.items():
        paths = find_cleanable_cache_dirs_in_roots(
            info.get("roots", []), include_named_cache_dirs=True
        )
        if not paths:
            continue
        s, i = clean_app_generic(
            f"{name} Cache",
            paths,
            info.get("procs"),
            dry_run=dry_run,
        )
        if i > 0:
            total_size += s
            total_items += i
            total_categories += 1
    return total_size, total_items, total_categories


def clean_flatpak_unused(dry_run=False):
    """Removes unused Flatpak runtimes from both installations.

    Only the --user installation used to be swept, which misses where the
    runtimes actually pile up on Debian and Ubuntu: Flathub's own setup
    instructions add the remote system-wide, so the unused runtimes sit in
    /var/lib/flatpak and clearing them needs root. The system pass is skipped
    when there is no system installation, so a user-only setup never triggers a
    password prompt for nothing.
    """
    if not shutil.which("flatpak"):
        return 0, 0
    if dry_run:
        print(f"  {SKIP} Flatpak runtimes would be checked")
        return 0, 0

    scopes = [("--user", False)]
    if Path("/var/lib/flatpak").is_dir():
        scopes.append(("--system", True))

    freed = 0
    uninstalled_any = False
    for scope, use_sudo in scopes:
        res = run_command(
            ["flatpak", "uninstall", scope, "--unused", "-y"],
            use_sudo=use_sudo,
            capture=True,
            env=C_LOCALE_ENV,
            timeout=PACKAGE_TRANSACTION_TIMEOUT,
        )
        if res.ok and res.stdout and "Uninstalling" in res.stdout:
            freed += parse_size_from_text(res.stdout)
            uninstalled_any = True

    if uninstalled_any:
        print(f"  {OK} Cleaned unused Flatpak runtimes ({bytes_to_human(freed)})")
        return freed, 1
    return 0, 0


def clean_generic_xdg_caches(days=CLEAN_CACHE_AGE_DAYS, dry_run=False):
    """Heuristic cleanup for unknown apps in ~/.cache."""
    cache_root = Path.home() / ".cache"
    if not cache_root.exists():
        return 0, 0
    total_size = 0
    total_items = 0
    try:
        for item in find_standard_cache_dirs(cache_root, max_depth=1):
            resolved = str(resolve_cache_path(item))
            if resolved in CLEANED_PATHS:
                continue
            register_cleaned_path(item)
            s = get_size_fast(item)
            removed = safe_remove(
                item,
                use_trash=False,
                dry_run=dry_run,
                known_size_bytes=s,
            )[0]
            if removed:
                total_size += s
                total_items += 1
                if not dry_run:
                    safe_name = sanitize_for_display(item.name)
                    print(f"  {OK} Tagged Cache: {safe_name} ({bytes_to_human(s)})")

        for candidate in find_xdg_cache_candidates(cache_root, days=days):
            item = candidate.path
            resolved = str(resolve_cache_path(item))
            if resolved in CLEANED_PATHS:
                continue
            s, i = clean_path_by_age(item, days=candidate.age_days, dry_run=dry_run)
            if i > 0:
                total_size += s
                total_items += i
                if not dry_run:
                    safe_name = sanitize_for_display(item.name)
                    print(f"  {OK} {candidate.label}: {safe_name} ({bytes_to_human(s)})")
    except OSError:
        pass
    if dry_run and total_size > 0:
        msg = f"  {SKIP} Other app caches ({bytes_to_human(total_size)}) would be checked"
        print(msg)
    return total_size, total_items


def clean_orphaned_remnants(dry_run=False, max_age_days=60):
    """Finds 'orphan' cache folders belonging to uninstalled software under ~/.cache.

    Safety Design Guarantees:
    1. NEVER scans ~/.config or ~/.local/share (user data and configurations are strictly protected).
    2. Collects all .desktop links across user and system directories (~/.local/share/applications,
       /usr/share/applications, /var/lib/flatpak/exports/share/applications).
    3. Requires cache folders to have no access/modification activity for at least `max_age_days` (default 60 days).
    """
    cache_root = Path.home() / ".cache"
    if not cache_root.exists():
        return 0, 0

    total_size = 0
    total_items = 0
    cutoff = age_cutoff(max_age_days)
    # Recoverable by default even though this is cache: "orphaned" is a heuristic
    # (a .desktop link can be missing for reasons other than the app being gone),
    # so a wrong match has to be undoable. config.json's use_trash=false opts out.
    use_trash = get_use_trash()

    # Core desktop and system infrastructure cache folders that must always be skipped
    system_folders = {
        # Audio & System Bus Infrastructure
        "pulse",
        "pipewire",
        "wireplumber",
        "alsa",
        "dbus",
        "dconf",
        "gnome-session",
        "systemd",
        "trash",
        "gvfs",
        "nautilus",
        "mime",
        "journal",
        # Desktop GUI Toolkit & Rendering (GNOME / KDE / GTK / Qt / XFCE)
        "gtk-2.0",
        "gtk-3.0",
        "gtk-4.0",
        "qt5",
        "qt6",
        "qtproject",
        "QtProject",
        "kde",
        "xfce4",
        "fontconfig",
        "fonts",
        "icons",
        "themes",
        "backgrounds",
        "applications",
        # CPU / GPU / Driver / Hardware Acceleration & Shaders
        "mesa_shader_cache",
        "mesa_shader_cache_db",
        "nvidia",
        "intel",
        "intel_gpu",
        "AMD",
        "opencl",
        "pocl",
        "vulkan",
        "gstreamer-1.0",
        # Package Managers, Sandboxes & Input Methods
        "flatpak",
        "common",
        "keyrings",
        "ibus",
        "fcitx",
        "fcitx5",
        "rime",
        "uim",
    }

    # Gather executable/link targets from all system and local .desktop files.
    # A missing directory here is not harmless: the cache folder of an app that
    # is still installed then looks orphaned and gets trashed. Snap keeps its
    # launchers in snapd's own export directory, and a --user flatpak install
    # exports under ~/.local/share/flatpak, neither of which the system-wide
    # flatpak path below covers.
    desktop_links: dict[str, str] = {}
    desktop_dirs = [
        Path.home() / ".local/share/applications",
        Path("/usr/share/applications"),
        Path("/usr/local/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local/share/flatpak/exports/share/applications",
        Path("/var/lib/snapd/desktop/applications"),
    ]
    for desktop_dir in desktop_dirs:
        if not desktop_dir.exists():
            continue
        try:
            for desktop_file in desktop_dir.glob("*.desktop"):
                exec_cmd = get_desktop_exec_command(desktop_file)
                if not exec_cmd:
                    continue
                stem = desktop_file.stem.lower()
                desktop_links[stem] = exec_cmd
                # The lookup below is keyed on the cache folder's name, which a
                # packaged entry's stem never equals: snapd generates
                # "<snap>_<app>.desktop" and flatpak exports "<app.id>.desktop",
                # while the app still caches into ~/.cache/<app>. Register those
                # components too, so an installed app's cache is not mistaken for
                # a remnant. setdefault keeps a real entry of that name winning.
                for part in (stem.split("_")[-1], stem.split(".")[-1]):
                    if part and part != stem:
                        desktop_links.setdefault(part, exec_cmd)
        except OSError:
            pass

    try:
        for item in cache_root.iterdir():
            if not item.is_dir() or item.name.startswith(".") or item.name in system_folders:
                continue
            resolved_item = item.resolve()
            if (
                str(resolved_item) in CLEANED_PATHS
                or resolved_item == DETECTED_APPS_FILE.parent.resolve()
            ):
                continue

            cmd_name = item.name.lower()

            # Check 1: Traditional Binary in PATH
            is_installed = any(
                shutil.which(c)
                for c in [cmd_name, cmd_name.split("-")[0], cmd_name.replace("-", "")]
            )

            # Check 2: AppImage / System Desktop Link
            if not is_installed:
                potential_path = desktop_links.get(cmd_name)
                if potential_path and (
                    shutil.which(potential_path) or Path(potential_path).exists()
                ):
                    is_installed = True

            if not is_installed:
                # Check 3: Safety Age Cutoff - skip recently touched/modified cache folders
                try:
                    st = item.stat()
                    if st.st_mtime > cutoff or st.st_atime > cutoff:
                        continue
                except OSError:
                    continue

                s = get_size_fast(item)
                if safe_remove(item, use_trash=use_trash, dry_run=dry_run, known_size_bytes=s)[0]:
                    total_size += s
                    total_items += 1
                    if not dry_run:
                        safe_name = sanitize_for_display(item.name)
                        msg = f"  {OK} Orphaned Cache Remnant: {safe_name} ({bytes_to_human(s)})"
                        print(msg)
    except OSError:
        pass

    if dry_run and total_size > 0:
        msg = (
            f"  {SKIP} Orphaned app cache remnants ({bytes_to_human(total_size)}) would be cleaned"
        )
        print(msg)
    return total_size, total_items


def clean_snap_cache(dry_run=False):
    """Cleans user caches for Snap applications under ~/snap/<app>/."""
    snap_root = Path.home() / "snap"
    if not snap_root.exists():
        return 0, 0

    total_size = 0
    total_items = 0
    try:
        for app_dir in snap_root.iterdir():
            if not app_dir.is_dir() or app_dir.name.startswith("."):
                continue

            # Skip if the snap app is currently running
            if is_app_running(app_dir.name):
                continue

            # $SNAP_USER_COMMON is the usual home for a snap's cache, but plenty
            # of snaps write into $SNAP_USER_DATA instead -- that is the revision
            # directory "current" points at -- so both have to be swept.
            app_size = 0
            for cache_path in (app_dir / "common" / ".cache", app_dir / "current" / ".cache"):
                if not cache_path.is_dir():
                    continue
                # For app cache dirs, clean all cache files.
                s, i = clean_path_by_age(cache_path, days=0, dry_run=dry_run)
                if i > 0:
                    total_size += s
                    total_items += i
                    app_size += s
            if not dry_run and app_size > 0:
                safe_name = sanitize_for_display(app_dir.name)
                print(f"  {OK} Snap Cache: {safe_name} ({bytes_to_human(app_size)})")
    except OSError:
        pass

    if dry_run and total_size > 0:
        print(f"  {SKIP} Snap application caches ({bytes_to_human(total_size)}) would be checked")
    return total_size, total_items


def _first_visit(path: Path, seen: set[Path]) -> bool:
    """Return True the first time *path* names a directory, recording it in *seen*.

    ~/.steam/steam is normally a symlink to ~/.local/share/Steam, so the native
    roots below name one directory that must not be swept -- or counted -- twice.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    if resolved in seen:
        return False
    seen.add(resolved)
    return True


def clean_steam_shader_cache(dry_run=False):
    """Clean Steam, Proton, NVIDIA, and Mesa shader caches."""
    total_size = 0
    total_items = 0
    home = Path.home()

    # Steam and the GPU drivers write into whatever HOME and XDG_CACHE_HOME the
    # process sees, and both sandbox formats move them: flatpak redirects the
    # cache to <app>/cache and snap to $SNAP_USER_COMMON/.cache. A flatpak or
    # snap Steam is exactly the install with tens of gigabytes of shader cache to
    # reclaim, and none of it sat under the native paths.
    flatpak_home = home / ".var" / "app" / "com.valvesoftware.Steam"
    snap_home = home / "snap" / "steam" / "common"
    steam_homes = [home, flatpak_home, snap_home]
    cache_homes = [home / ".cache", flatpak_home / "cache", snap_home / ".cache"]
    # Steam's data directory holds both the shader cache and the Proton prefixes.
    steam_roots = [
        home / ".steam" / "steam",
        *(steam_home / ".local" / "share" / "Steam" for steam_home in steam_homes),
    ]

    shader_paths = [
        *(root / "shadercache" for root in steam_roots),
        *(steam_home / ".nv" / "ComputeCache" for steam_home in steam_homes),
        *(steam_home / ".nv" / "GLCache" for steam_home in steam_homes),
        *(cache_home / "nvidia" / "ComputeCache" for cache_home in cache_homes),
        *(cache_home / "nvidia" / "GLCache" for cache_home in cache_homes),
        *(cache_home / "mesa_shader_cache" for cache_home in cache_homes),
        *(cache_home / "mesa_shader_cache_db" for cache_home in cache_homes),
    ]
    seen: set[Path] = set()
    for shader_dir in shader_paths:
        if not shader_dir.is_dir() or not _first_visit(shader_dir, seen):
            continue
        s, i = clean_path_by_age(shader_dir, days=30, dry_run=dry_run)
        if i > 0:
            total_size += s
            total_items += i

    # Proton/Wine prefix shader caches
    for steam_root in steam_roots:
        compatdata = steam_root / "steamapps" / "compatdata"
        if not compatdata.is_dir() or not _first_visit(compatdata, seen):
            continue
        try:
            for prefix_dir in compatdata.iterdir():
                shader_cache = prefix_dir / "pfx" / "drive_c" / "windows" / "temp"
                if shader_cache.is_dir():
                    s, i = clean_path_by_age(shader_cache, days=30, dry_run=dry_run)
                    if i > 0:
                        total_size += s
                        total_items += i
        except OSError:
            pass

    if total_items > 0:
        glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
        print(f"  {glyph} GPU/Steam/Proton shader cache ({bytes_to_human(total_size)}) {status}")
    return total_size, total_items


def clean_ide_caches(dry_run=False):
    """Clean IDE and code editor caches."""
    total_size = 0
    total_items = 0
    home = Path.home()

    # Static known IDE cache paths. Each editor is listed by its config
    # directory name, then expanded across the native and flatpak locations:
    # flatpak redirects XDG_CONFIG_HOME into ~/.var/app/<id>/config, so a flatpak
    # VS Code -- and CachedData alone routinely reaches a gigabyte -- was
    # completely out of reach. The snap builds of these editors all use classic
    # confinement and so write to the real ~/.config, which is already covered.
    editor_config_dirs = [
        ("Code", "com.visualstudio.code"),
        ("VSCodium", "com.vscodium.codium"),
        ("Cursor", None),
    ]
    cache_subdirs = ("CachedData", "CachedExtensionVSIXs", "Cache")

    ide_caches: list[Path] = []
    for config_name, flatpak_id in editor_config_dirs:
        config_roots = [home / ".config" / config_name]
        if flatpak_id:
            config_roots.append(home / ".var" / "app" / flatpak_id / "config" / config_name)
        ide_caches.extend(root / subdir for root in config_roots for subdir in cache_subdirs)

    for cache_dir in ide_caches:
        if cache_dir.is_dir():
            s, i = clean_path_by_age(cache_dir, days=30, dry_run=dry_run)
            if i > 0:
                total_size += s
                total_items += i

    # JetBrains IDEs: ~/.cache/JetBrains/*/
    jetbrains_cache = home / ".cache" / "JetBrains"
    if jetbrains_cache.is_dir():
        try:
            for ide_dir in jetbrains_cache.iterdir():
                if ide_dir.is_dir():
                    s, i = clean_path_by_age(ide_dir, days=30, dry_run=dry_run)
                    if i > 0:
                        total_size += s
                        total_items += i
        except OSError:
            pass

    if total_items > 0:
        glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
        print(f"  {glyph} IDE/Editor caches ({bytes_to_human(total_size)}) {status}")
    return total_size, total_items


def clean_desktop_apps_caches(
    detected_apps: dict[str, dict[str, Any]], dry_run: bool = False
) -> tuple[int, int]:
    """Cleans deep caches for statically defined and auto-discovered desktop applications."""
    total_size = 0
    total_items = 0
    all_apps = {**get_desktop_app_cleanup_defs(), **detected_apps}
    for name, info in all_apps.items():
        s, i = clean_app_generic(name, info["paths"], info.get("procs"), dry_run=dry_run)
        if i > 0:
            total_size += s
            total_items += i
    return total_size, total_items


class AppCleanerRegistry:
    """Registry and pipeline for deep application cache cleaners."""

    cleaners: list[Callable[..., tuple[int, int] | tuple[int, int, int]]] = []

    @classmethod
    def register(
        cls, func: Callable[..., tuple[int, int] | tuple[int, int, int]]
    ) -> Callable[..., tuple[int, int] | tuple[int, int, int]]:
        cls.cleaners.append(func)
        return func


register_app_cleaner = AppCleanerRegistry.register

# Register individual sub-cleaners into the deep app cleaning pipeline
register_app_cleaner(clean_browser_caches)
register_app_cleaner(clean_flatpak_unused)
register_app_cleaner(clean_snap_cache)
register_app_cleaner(clean_generic_xdg_caches)
register_app_cleaner(clean_orphaned_remnants)
register_app_cleaner(clean_steam_shader_cache)
register_app_cleaner(clean_ide_caches)


def _run_registered_cleaners(dry_run: bool) -> tuple[int, int, int]:
    """Every registered sub-cleaner, run in registration order and totalled.

    A cleaner returns either two values or three. The third is how many
    categories it reported; a two-value cleaner counts as one category when it
    removed anything and none when it removed nothing, which is what the
    registry's older cleaners relied on before the third value existed.
    """
    total_size = 0
    total_items = 0
    total_categories = 0
    for cleaner in AppCleanerRegistry.cleaners:
        result = cleaner(dry_run=dry_run)
        size, items = result[0], result[1]
        categories = result[2] if len(result) >= 3 else (1 if items > 0 else 0)

        total_size += size
        total_items += items
        total_categories += categories
    return total_size, total_items, total_categories


def clean_apps_deep(
    dry_run: bool = False, detected_apps: dict[str, dict[str, Any]] | None = None
) -> tuple[int, int, int]:
    """Deep cleanup for installed apps, browsers, IDEs, Flatpak/Snap, games, and XDG remnants."""
    if detected_apps is None:
        detected_apps = proactive_app_detection()

    total_size, total_items = clean_desktop_apps_caches(
        detected_apps=detected_apps, dry_run=dry_run
    )
    total_categories = 1 if total_items > 0 else 0

    pipeline_size, pipeline_items, pipeline_categories = _run_registered_cleaners(dry_run)
    total_size += pipeline_size
    total_items += pipeline_items
    total_categories += pipeline_categories

    return total_size, total_items, total_categories
