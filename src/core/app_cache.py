import os
from dataclasses import dataclass
from pathlib import Path

from .browser_cache import CLEANABLE_APP_CACHE_DIR_NAMES
from .desktop_app_cache import is_desktop_app_cache_path
from .file_ops import has_valid_cachedir_tag
from .whitelist import (
    get_hard_protection_reason,
    is_cleanable_linux_app_data,
    is_sensitive_linux_app_data,
)

GENERIC_XDG_CACHE_NAME_KEYWORDS = ("cache", "log", "tmp", "temp")


@dataclass(frozen=True)
class XdgCacheCandidate:
    path: Path
    age_days: int
    label: str


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


def is_standard_cache_dir(path: str | Path) -> bool:
    return has_valid_cachedir_tag(path)


def is_known_app_cache_path(path: str | Path) -> bool:
    return is_cleanable_linux_app_data(path) or is_desktop_app_cache_path(path)


def get_xdg_cache_root() -> Path:
    return Path.home() / ".cache"


def is_generic_xdg_cache_path(path: str | Path) -> bool:
    raw_path = Path(path).expanduser()
    if raw_path.is_symlink():
        return False

    resolved_path = resolve_cache_path(raw_path)
    resolved_root = resolve_cache_path(get_xdg_cache_root())
    return resolved_path != resolved_root and resolved_root in resolved_path.parents


def get_cache_cleanable_reason(path: str | Path) -> str:
    if is_standard_cache_dir(path):
        return "CACHEDIR.TAG"
    if is_known_app_cache_path(path):
        return "App cache"
    if is_generic_xdg_cache_path(path):
        return "XDG cache"
    return ""


def is_obvious_xdg_cache_name(name: str) -> bool:
    lowered = name.lower()
    return any(keyword in lowered for keyword in GENERIC_XDG_CACHE_NAME_KEYWORDS)


def get_xdg_cache_candidate(path: str | Path, *, days: int = 30) -> XdgCacheCandidate | None:
    raw_path = Path(path).expanduser()
    if not raw_path.is_dir() or not is_generic_xdg_cache_path(raw_path):
        return None

    if is_obvious_xdg_cache_name(raw_path.name):
        return XdgCacheCandidate(path=raw_path, age_days=min(days, 3), label="Generic Cache")
    return XdgCacheCandidate(path=raw_path, age_days=days, label="Stale App Data")


def find_xdg_cache_candidates(
    root: str | Path | None = None, *, days: int = 30
) -> list[XdgCacheCandidate]:
    cache_root = resolve_cache_root(root) if root is not None else get_xdg_cache_root()
    resolved_root = resolve_cache_path(cache_root)
    if not cache_root.is_dir() or get_hard_protection_reason(resolved_root) is not None:
        return []

    candidates: list[XdgCacheCandidate] = []
    try:
        for item in cache_root.iterdir():
            if item.is_symlink() or not item.is_dir() or is_standard_cache_dir(item):
                continue
            candidate = get_xdg_cache_candidate(item, days=days)
            if candidate is not None:
                candidates.append(candidate)
    except OSError:
        return []
    return candidates


def _walk_depth(root: Path, current: Path) -> int:
    try:
        return len(current.relative_to(root).parts)
    except ValueError:
        return 0


def find_standard_cache_dirs(
    root: str | Path,
    *,
    include_root: bool = False,
    max_depth: int | None = None,
) -> list[Path]:
    raw_root = resolve_cache_root(root)
    resolved_root = resolve_cache_path(raw_root)
    if not raw_root.is_dir() or get_hard_protection_reason(resolved_root) is not None:
        return []

    cache_paths: list[Path] = []
    seen: set[Path] = set()
    for current, dirnames, _filenames in os.walk(raw_root):
        current_path = Path(current)
        depth = _walk_depth(raw_root, current_path)
        if max_depth is not None and depth >= max_depth:
            dirnames[:] = []

        resolved_current = resolve_cache_path(current_path)
        if (
            (include_root or current_path != raw_root)
            and resolved_current not in seen
            and is_standard_cache_dir(current_path)
        ):
            cache_paths.append(current_path)
            seen.add(resolved_current)
            dirnames[:] = []
            continue

        for dirname in list(dirnames):
            child = current_path / dirname
            if child.is_symlink() or get_hard_protection_reason(resolve_cache_path(child)):
                dirnames.remove(dirname)

    return cache_paths


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
            if child.is_symlink():
                dirnames.remove(dirname)
                continue
            resolved_child = resolve_cache_path(child)
            if resolved_child in seen:
                dirnames.remove(dirname)
                continue

            if (
                is_standard_cache_dir(resolved_child)
                or is_known_app_cache_path(resolved_child)
                or (include_named_cache_dirs and is_named_cache_dir(child))
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
