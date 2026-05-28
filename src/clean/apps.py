import json
import shutil
from pathlib import Path

from ..core.constants import APP_DEFS, DETECTED_APPS_FILE
from ..core.file_ops import (
    CLEANED_PATHS,
    bytes_to_human,
    clean_path_by_age,
    get_size,
    is_app_running,
    parse_size_from_text,
    register_cleaned_path,
    safe_remove,
)
from ..core.system import run_command


def proactive_app_detection():
    """Scans for installed apps and matches them with their folders. Also prunes dead entries."""
    detected = {}
    if DETECTED_APPS_FILE.exists():
        try:
            with open(DETECTED_APPS_FILE) as f:
                detected = json.load(f)
        except Exception:
            pass

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
    handled_names = {n.lower() for n in APP_DEFS}
    handled_names.update(n.lower() for n in detected)

    new_found = False
    for root_str in ["~/.cache", "~/.config"]:
        root = Path(root_str).expanduser()
        if not root.exists():
            continue
        try:
            for item in root.iterdir():
                if not item.is_dir() or item.name.startswith("."):
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
        except Exception:
            pass

    # Save if we found NEW things OR if we PRUNED old things
    if new_found or len(detected) != original_count or not DETECTED_APPS_FILE.exists():
        try:
            DETECTED_APPS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(DETECTED_APPS_FILE, "w") as f:
                json.dump(detected, f, indent=2)
            if new_found:
                msg = (
                    f"  \033[1;90mℹ️  Updated local app registry ({len(detected)} apps known)\033[0m"
                )
                print(msg)
        except Exception:
            pass
    return detected


def clean_app_generic(name, paths, process_names=None, dry_run=False):
    """Unified cleaner for any app with process safety."""
    if process_names and any(is_app_running(p) for p in process_names):
        print(f"  \033[0;90m◎\033[0m {name} is running · cleanup skipped")
        return 0, 0

    total_freed = 0
    items_cleaned = 0
    found = False
    for p_str in paths:
        path = Path(p_str).expanduser().resolve()
        register_cleaned_path(path)
        if path.exists():
            found = True
            size = get_size(path)
            if dry_run:
                total_freed += size
                items_cleaned += 1
                continue
            try:
                if path.is_dir():
                    for item in path.iterdir():
                        s = get_size(item)
                        if safe_remove(item, use_trash=False)[0]:
                            total_freed += s
                            items_cleaned += 1
                else:
                    if safe_remove(path, use_trash=False)[0]:
                        total_freed += size
                        items_cleaned += 1
            except Exception:
                continue

    if found and (total_freed > 0 or dry_run):
        status = "would be cleaned" if dry_run else "cache cleaned"
        print(f"  \033[0;32m✓\033[0m {name} ({bytes_to_human(total_freed)}) {status}")
        return total_freed, items_cleaned
    return 0, 0


def clean_flatpak_unused(dry_run=False):
    """Removes unused Flatpak runtimes."""
    if shutil.which("flatpak"):
        if dry_run:
            print("  \033[0;32m✓\033[0m Flatpak runtimes would be checked")
            return 0, 0
        res = run_command(["flatpak", "uninstall", "--unused", "-y"], use_sudo=False, capture=True)
        if res and res.stdout and "Uninstalling" in res.stdout:
            freed = parse_size_from_text(res.stdout)
            msg = f"  \033[0;32m✓\033[0m Cleaned unused Flatpak runtimes ({bytes_to_human(freed)})"
            print(msg)
            return freed, 1
    return 0, 0


def clean_generic_xdg_caches(days=30, dry_run=False):
    """Heuristic cleanup for unknown apps in ~/.cache."""
    cache_root = Path.home() / ".cache"
    if not cache_root.exists():
        return 0, 0
    total_size = 0
    total_items = 0
    try:
        for item in cache_root.iterdir():
            if not item.is_dir() or str(item.resolve()) in CLEANED_PATHS:
                continue
            is_obvious_junk = any(kw in item.name.lower() for kw in ["cache", "log", "tmp", "temp"])
            s, i = clean_path_by_age(item, days=0 if is_obvious_junk else days, dry_run=dry_run)
            if i > 0:
                total_size += s
                total_items += i
                if not dry_run:
                    tag = "Generic Cache" if is_obvious_junk else "Stale App Data"
                    print(f"  \033[0;32m✓\033[0m {tag}: {item.name} ({bytes_to_human(s)})")
    except Exception:
        pass
    if dry_run and total_size > 0:
        msg = (
            f"  \033[0;32m✓\033[0m Other app caches ({bytes_to_human(total_size)}) would be checked"
        )
        print(msg)
    return total_size, total_items


def clean_orphaned_remnants(dry_run=False):
    """Finds 'orphan' folders belonging to uninstalled software, including AppImages."""
    search_roots = [Path.home() / ".config", Path.home() / ".cache", Path.home() / ".local/share"]
    total_size = 0
    total_items = 0
    system_folders = {
        "pulse",
        "dbus",
        "dconf",
        "gnome-session",
        "gtk-3.0",
        "gtk-4.0",
        "fontconfig",
        "mime",
        "systemd",
        "trash",
        "applications",
        "icons",
        "themes",
        "backgrounds",
        "flatpak",
        "gvfs",
        "ibus",
        "nautilus",
        "common",
    }

    # Pre-scan desktop files to find AppImage paths
    desktop_links = {}
    desktop_dir = Path.home() / ".local/share/applications"
    if desktop_dir.exists():
        try:
            for d in desktop_dir.glob("*.desktop"):
                with open(d, errors="ignore") as f:
                    content = f.read()
                    exec_line = [line for line in content.splitlines() if line.startswith("Exec=")]
                    if exec_line:
                        # Extract the path, removing args
                        path_part = exec_line[0].split("=")[1].split()[0].strip("\"'")
                        desktop_links[d.stem.lower()] = path_part
        except Exception:
            pass

    for root in search_roots:
        if not root.exists():
            continue
        try:
            for item in root.iterdir():
                if not item.is_dir() or item.name.startswith(".") or item.name in system_folders:
                    continue
                if (
                    str(item.resolve()) in CLEANED_PATHS
                    or item.resolve() == DETECTED_APPS_FILE.parent.resolve()
                ):
                    continue

                cmd_name = item.name.lower()

                # Check 1: Traditional Binary
                is_installed = any(
                    shutil.which(c)
                    for c in [cmd_name, cmd_name.split("-")[0], cmd_name.replace("-", "")]
                )

                # Check 2: AppImage / Desktop link
                if not is_installed:
                    # Look if any desktop file points to a missing file for this app name
                    potential_path = desktop_links.get(cmd_name)
                    if potential_path and Path(potential_path).exists():
                        is_installed = True

                if not is_installed:
                    # Final Safety: 60 days for unidentified orphans
                    s, i = clean_path_by_age(item, days=60, dry_run=dry_run)
                    if i > 0:
                        total_size += s
                        total_items += i
                        if not dry_run:
                            msg = f"  \033[0;32m✓\033[0m Orphaned Remnant: {item.name} ({bytes_to_human(s)})"
                            print(msg)
        except Exception:
            pass
    if dry_run and total_size > 0:
        msg = f"  \033[0;32m✓\033[0m Orphaned app remnants ({bytes_to_human(total_size)}) would be checked"
        print(msg)
    return total_size, total_items


def clean_apps_deep(dry_run=False):
    """Main entry point for deep application cleanup."""
    total_size = 0
    total_items = 0
    total_categories = 0
    detected_apps = proactive_app_detection()

    # Combined loop for defined and detected apps
    all_apps = {**APP_DEFS, **detected_apps}
    for name, info in all_apps.items():
        s, i = clean_app_generic(name, info["paths"], info.get("procs"), dry_run=dry_run)
        if i > 0:
            total_size += s
            total_items += i
            total_categories += 1

    for func in [clean_flatpak_unused, clean_generic_xdg_caches, clean_orphaned_remnants]:
        s, i = func(dry_run=dry_run)[:2]
        if i > 0:
            total_size += s
            total_items += i
            total_categories += 1
    return total_size, total_items, total_categories
