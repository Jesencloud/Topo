import os
from pathlib import Path

from .browser_cache import CLEANABLE_APP_CACHE_DIR_NAMES
from .whitelist import (
    get_hard_protection_reason,
    is_cleanable_linux_app_data,
    is_sensitive_linux_app_data,
)


def resolve_cache_path(path: str | Path) -> Path:
    raw_path = Path(path).expanduser()
    try:
        return raw_path.resolve(strict=False)
    except OSError:
        return raw_path.absolute()


def resolve_cache_root(path: str | Path) -> Path:
    root = Path(path).expanduser()
    if root.is_absolute():
        return root
    return Path.home() / root


def is_named_cache_dir(path: str | Path) -> bool:
    return Path(path).name in CLEANABLE_APP_CACHE_DIR_NAMES


def find_cleanable_cache_dirs(
    root: str | Path,
    *,
    include_named_cache_dirs: bool = False,
    require_sensitive_app_data_root: bool = False,
) -> list[Path]:
    """Find cache-like descendants under one app/profile root.

    `include_named_cache_dirs` is for known browser cache roots such as
    ~/.cache/mozilla where paths are not sensitive profile data but directory names
    like cache2 are still safe cleanup targets.
    """
    raw_root = resolve_cache_root(root)
    resolved_root = resolve_cache_path(raw_root)
    if (
        not raw_root.is_dir()
        or get_hard_protection_reason(resolved_root) is not None
        or is_cleanable_linux_app_data(resolved_root)
    ):
        return []
    if require_sensitive_app_data_root and not is_sensitive_linux_app_data(resolved_root):
        return []

    cache_paths: list[Path] = []
    seen: set[Path] = set()
    for current, dirnames, _filenames in os.walk(raw_root):
        for dirname in list(dirnames):
            child = Path(current) / dirname
            resolved_child = resolve_cache_path(child)
            if resolved_child in seen:
                dirnames.remove(dirname)
                continue

            if is_cleanable_linux_app_data(resolved_child) or (
                include_named_cache_dirs and is_named_cache_dir(child)
            ):
                cache_paths.append(child)
                seen.add(resolved_child)
                dirnames.remove(dirname)

    return cache_paths


def find_cleanable_cache_dirs_in_roots(
    roots,
    *,
    include_named_cache_dirs: bool = False,
    require_sensitive_app_data_root: bool = False,
) -> list[Path]:
    cache_paths: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        for cache_path in find_cleanable_cache_dirs(
            root,
            include_named_cache_dirs=include_named_cache_dirs,
            require_sensitive_app_data_root=require_sensitive_app_data_root,
        ):
            resolved = resolve_cache_path(cache_path)
            if resolved not in seen:
                cache_paths.append(cache_path)
                seen.add(resolved)
    return cache_paths
