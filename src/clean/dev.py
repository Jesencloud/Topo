import shutil
from pathlib import Path

from ..core.constants import (
    CLEAN_CARGO_AGE_DAYS,
    DEFAULT_PROJECT_SEARCH_PATHS,
    DEV_CACHES,
    OK,
    SKIP,
)
from ..core.file_ops import (
    bytes_to_human,
    clean_path_by_age,
    get_size_fast,
    register_cleaned_path,
    safe_remove,
)
from ..core.heavy_cache import get_ai_model_cleanup_defs, get_container_cache_def
from ..core.system import run_command


def clean_tool_cache(description, command_args, cache_path=None, dry_run=False):
    """Helper to clean a specific tool's cache with verified success."""
    total_size = 0
    if cache_path:
        path = Path(cache_path).expanduser()
        if path.exists():
            total_size = get_size_fast(path)
        register_cleaned_path(cache_path)

    if dry_run:
        if total_size > 0 or not cache_path:
            print(f"  {SKIP} {description} ({bytes_to_human(total_size)}) would be cleaned")
            return total_size, 1
        return 0, 0

    if total_size > 0 or not cache_path:
        res = run_command(command_args, capture=True)
        cache_gone = cache_path and not Path(cache_path).expanduser().exists()
        if (res and res.returncode == 0) or cache_gone:
            # Report space actually reclaimed (before - after), not the pre-clean
            # size, since `npm/pip/go cache clean` may only clear part of it.
            freed = total_size
            if cache_path and not cache_gone:
                after = get_size_fast(Path(cache_path).expanduser())
                freed = max(0, total_size - after)
            print(f"  {OK} {description} ({bytes_to_human(freed)}) cleaned")
            return freed, 1
    return 0, 0


def clean_docker(dry_run=False):
    """Clean unused Docker data."""
    if shutil.which("docker"):
        if dry_run:
            print(f"  {SKIP} Docker (unused images/volumes) would be pruned")
            return 0, 1
        use_sudo = True
        if run_command(["docker", "info"], capture=True, timeout=10).ok:
            use_sudo = False
        res = run_command(
            ["docker", "system", "prune", "-f", "--volumes"], use_sudo=use_sudo, capture=True
        )
        if res and res.returncode == 0:
            print(f"  {OK} Docker system pruned")
            return 0, 1
    return 0, 0


def clean_podman(dry_run=False):
    """Clean unused Podman data and caches."""
    total_size = 0
    items = 0
    if shutil.which("podman"):
        if dry_run:
            print(f"  {SKIP} Podman (unused images/volumes) would be pruned")
            items += 1
        else:
            res = run_command(["podman", "system", "prune", "-f"], capture=True)
            if res and res.returncode == 0:
                print(f"  {OK} Podman system pruned")
                items += 1

        # Clean storage cache
        cache_path = get_container_cache_def("podman-cache").resolved_path()
        if cache_path.exists():
            register_cleaned_path(cache_path)
            s, i = clean_path_by_age(cache_path, days=0, dry_run=dry_run)
            total_size += s
            items += i
            if i > 0 and not dry_run:
                print(f"  {OK} Podman transfer cache ({bytes_to_human(s)}) cleaned")
    return total_size, items


def clean_multipass(dry_run=False):
    """Purges deleted Multipass instances."""
    if shutil.which("multipass"):
        if dry_run:
            print(f"  {SKIP} Multipass deleted instances would be purged")
            return 0, 1
        res = run_command(["multipass", "purge"], capture=True)
        if res and res.returncode == 0:
            print(f"  {OK} Multipass purged")
            return 0, 1
    return 0, 0


def clean_ai_models(dry_run=False):
    """Clean heavy AI model hubs with age awareness."""
    total_size = 0
    total_items = 0

    for target in get_ai_model_cleanup_defs():
        path = target.resolved_path()
        register_cleaned_path(path)
        s, i = clean_path_by_age(path, days=target.age_days, dry_run=dry_run)
        if i > 0:
            total_size += s
            total_items += i
            glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
            print(f"  {glyph} {target.label} ({bytes_to_human(s)}) {status}")
    return total_size, total_items


def clean_java_caches(dry_run=False):
    """Clean Gradle and Maven build caches."""
    total_size = 0
    total_items = 0
    home = Path.home()

    targets = [
        ("Gradle caches", home / ".gradle" / "caches"),
        ("Gradle wrapper", home / ".gradle" / "wrapper" / "dists"),
        ("Maven repository", home / ".m2" / "repository"),
    ]
    for label, cache_path in targets:
        if not cache_path.is_dir():
            continue
        register_cleaned_path(cache_path)
        s, i = clean_path_by_age(cache_path, days=60, dry_run=dry_run)
        if i > 0:
            total_size += s
            total_items += i
            glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
            print(f"  {glyph} {label} ({bytes_to_human(s)}) {status}")
    return total_size, total_items


def clean_python_pycache(dry_run=False):
    """Clean __pycache__ directories from user project directories."""
    total_size = 0
    total_items = 0

    for search_path in DEFAULT_PROJECT_SEARCH_PATHS:
        root = Path(search_path)
        if not root.is_dir():
            continue
        try:
            for pycache in root.rglob("__pycache__"):
                if not pycache.is_dir():
                    continue
                size = get_size_fast(pycache)
                if safe_remove(pycache, use_trash=False, dry_run=dry_run)[0]:
                    total_size += size
                    total_items += 1
        except OSError:
            continue

    if total_items > 0:
        glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
        print(f"  {glyph} Python __pycache__ ({bytes_to_human(total_size)}) {status}")
    return total_size, total_items


def clean_package_manager_caches(dry_run: bool = False) -> tuple[int, int]:
    """Clean standard developer package manager caches (npm, pip, go)."""
    total_size = 0
    total_items = 0
    pm_tools = [
        ("npm cache", ["npm", "cache", "clean", "--force"], DEV_CACHES["npm"]),
        ("pip cache", ["pip3", "cache", "purge"], DEV_CACHES["pip"]),
        ("go cache", ["go", "clean", "-cache"], DEV_CACHES["go"]),
    ]
    for desc, cmd, path in pm_tools:
        if shutil.which(cmd[0]):
            s, i = clean_tool_cache(desc, cmd, path, dry_run)
            if i > 0:
                total_size += s
                total_items += i
    return total_size, total_items


def clean_cargo_cache(dry_run: bool = False) -> tuple[int, int]:
    """Clean Rust Cargo registry cache based on age."""
    cargo_path = DEV_CACHES["cargo"]
    if not cargo_path.exists():
        return 0, 0

    register_cleaned_path(cargo_path)
    s, i = clean_path_by_age(cargo_path, days=CLEAN_CARGO_AGE_DAYS, dry_run=dry_run)
    if i > 0:
        glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
        print(f"  {glyph} Cargo cache ({bytes_to_human(s)}) {status}")
        return s, i
    return 0, 0


def clean_container_and_virtualization_caches(dry_run: bool = False) -> tuple[int, int]:
    """Clean Docker, Podman, and Multipass virtualization caches."""
    total_size = 0
    total_items = 0
    for func in [clean_docker, clean_podman, clean_multipass]:
        s, i = func(dry_run=dry_run)[:2]
        if i > 0:
            total_size += s
            total_items += i
    return total_size, total_items


def clean_developer_tools(dry_run: bool = False) -> tuple[int, int, int]:
    """Main entry for developer-focused cleanup pipeline."""
    total_size = 0
    total_items = 0
    total_categories = 0

    dev_sub_cleaners = [
        clean_package_manager_caches,
        clean_cargo_cache,
        clean_java_caches,
        clean_python_pycache,
        clean_ai_models,
        clean_container_and_virtualization_caches,
    ]

    for cleaner in dev_sub_cleaners:
        s, i = cleaner(dry_run=dry_run)[:2]
        if i > 0:
            total_size += s
            total_items += i
            total_categories += 1

    return total_size, total_items, total_categories
