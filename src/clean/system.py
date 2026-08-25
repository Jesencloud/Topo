import platform
import shutil
from pathlib import Path

from ..core.constants import OK
from ..core.file_ops import bytes_to_human, get_size_fast, parse_size_from_text, safe_remove
from ..core.heavy_cache import PACKAGE_MANAGER_CACHE_DEFS
from ..core.package_manager import detect_package_manager, resolve_admin_tool
from ..core.system import (
    APT_NONINTERACTIVE_ENV,
    C_LOCALE_ENV,
    get_os_id,
    run_command,
)


class DryRunReporter:
    """Helper to handle uniform output reporting across dry-run and actual execution modes."""

    @staticmethod
    def report(
        action_name: str,
        freed_bytes: int = 0,
        items_count: int = 0,
        dry_run: bool = False,
    ) -> tuple[int, int, int]:
        if freed_bytes == 0 and items_count == 0:
            return 0, 0, 0

        size_str = f" ({bytes_to_human(freed_bytes)})" if freed_bytes > 0 else ""
        items_str = f" ({items_count} items)" if items_count > 0 and freed_bytes == 0 else ""

        if dry_run:
            print(f"  {OK} {action_name}{size_str}{items_str} would be cleaned")
        else:
            print(f"  {OK} Cleaned {action_name}{size_str}{items_str}")

        return freed_bytes, items_count, 1


def clean_snaps(dry_run: bool = False) -> tuple[int, int, int]:
    """Removes old revisions of snaps to save massive space on Ubuntu."""
    if not shutil.which("snap"):
        return 0, 0, 0

    if dry_run:
        print(f"  {OK} Old Snap revisions would be removed")
        return 0, 0, 1

    # The revision table is matched on the English word "disabled".
    res = run_command(["snap", "list", "--all"], capture=True, env=C_LOCALE_ENV)
    if not res or not res.stdout:
        return 0, 0, 0

    count = 0
    for line in res.stdout.splitlines():
        if "disabled" in line:
            parts = line.split()
            if len(parts) >= 3:
                rm_res = run_command(
                    ["snap", "remove", parts[0], "--revision", parts[2]],
                    use_sudo=True,
                    capture=True,
                )
                if rm_res.ok:
                    count += 1

    if count > 0:
        print(f"  {OK} Removed {count} old Snap revisions")
        return 0, count, 1
    return 0, 0, 0


def _get_package_manager_cache_paths(cleaner_key: str) -> list[Path]:
    """The cache directories to measure, from the table Analyze reads too.

    Every path of the family that exists, not just the first: dnf5 moved the
    cache to /var/cache/libdnf5 and leaves the old /var/cache/dnf behind, and one
    `dnf clean packages` empties both. apt's own `partial/` subdirectory is
    deliberately not listed -- get_size_fast() already recurses into it, so
    naming it separately only counted its bytes twice.
    """
    for definition in PACKAGE_MANAGER_CACHE_DEFS:
        if definition.key == cleaner_key:
            candidates = (definition.path, *definition.fallback_paths)
            return [path for path in map(Path, candidates) if path.exists()]
    return []


def _measure_package_cache_size(cache_paths: list[Path]) -> int:
    """Measures total bytes stored in package manager cache directories."""
    return sum(get_size_fast(p) for p in cache_paths if p.exists())


def clean_package_manager(dry_run: bool = False) -> tuple[int, int, int]:
    """Clean system package manager caches."""
    manager = detect_package_manager(get_os_id())
    if manager is None:
        return 0, 0, 0

    freed = 0
    snap_items = 0
    snap_cats = 0
    if manager.key == "apt":
        s, snap_items, snap_cats = clean_snaps(dry_run=dry_run)
        freed += s

    # The resolved tool, so a dnf5-only box is cleaned rather than skipped -- and
    # so the skip below asks about the binary that would actually run.
    tool = resolve_admin_tool(manager)
    if not shutil.which(tool):
        return freed, snap_items, snap_cats

    cache_paths = _get_package_manager_cache_paths(manager.key)
    pre_size = _measure_package_cache_size(cache_paths)

    if dry_run:
        size_hint = f" ({bytes_to_human(pre_size)})" if pre_size > 0 else ""
        print(f"  {OK} {manager.label} cache{size_hint} would be cleaned")
        return freed + pre_size, snap_items, snap_cats + 1

    res = run_command(
        [tool, *manager.cache_clean_args], use_sudo=True, capture=True, env=C_LOCALE_ENV
    )
    post_size = _measure_package_cache_size(cache_paths)
    measured_freed = max(0, pre_size - post_size)

    if measured_freed > 0:
        freed += measured_freed
    elif res.ok and res.stdout:
        freed += parse_size_from_text(res.stdout)

    if res.ok:
        freed_str = f" ({bytes_to_human(freed)})" if freed > 0 else ""
        print(f"  {OK} Cleaned {manager.label} cache{freed_str}")
        return freed, snap_items + 1, snap_cats + 1

    return freed, snap_items, snap_cats


def clean_journal(dry_run: bool = False) -> tuple[int, int, int]:
    """Vacuum systemd journal logs."""
    if not shutil.which("journalctl"):
        return 0, 0, 0

    if dry_run:
        print(f"  {OK} journal logs would be vacuumed")
        return 0, 0, 1

    res = run_command(
        ["journalctl", "--vacuum-size=1M"], use_sudo=True, capture=True, env=C_LOCALE_ENV
    )
    if res.ok and res.stdout:
        freed = parse_size_from_text(res.stdout)
        if freed > 0:
            print(f"  {OK} Vacuumed journal logs ({bytes_to_human(freed)})")
            return freed, 1, 1
    return 0, 0, 0


def clean_orphaned_packages(dry_run: bool = False) -> tuple[int, int, int]:
    """Remove orphaned dependencies that are no longer needed."""
    manager = detect_package_manager(get_os_id())
    if manager is None or not manager.orphan_removal:
        return 0, 0, 0

    tool = resolve_admin_tool(manager)
    if not shutil.which(tool):
        return 0, 0, 0

    if manager.key == "apt":
        if dry_run:
            print(f"  {OK} Orphaned {manager.label} packages would be autoremoved")
            return 0, 0, 1
        res = run_command(
            [tool, "autoremove", "-y"],
            use_sudo=True,
            capture=True,
            env=APT_NONINTERACTIVE_ENV,
        )
        if res.ok:
            freed = parse_size_from_text(res.stdout)
            print(f"  {OK} Removed orphaned {manager.label} packages")
            return freed, 1, 1

    elif manager.key == "dnf":
        if dry_run:
            print(f"  {OK} Orphaned {manager.label} packages would be autoremoved")
            return 0, 0, 1
        res = run_command([tool, "autoremove", "-y"], use_sudo=True, capture=True, env=C_LOCALE_ENV)
        if res.ok:
            freed = parse_size_from_text(res.stdout)
            items = res.stdout.count("\n") // 2
            print(f"  {OK} Removed orphaned {manager.label} packages ({bytes_to_human(freed)})")
            return freed, items, 1

    elif manager.key == "pacman":
        list_res = run_command([tool, "-Qtdq"], capture=True)
        if list_res.ok and list_res.stdout.strip():
            orphans = list_res.stdout.split()
            if dry_run:
                print(f"  {OK} {len(orphans)} orphaned {manager.label} packages would be removed")
                return 0, 0, 1
            remove_res = run_command(
                [tool, "-Rns", "--noconfirm"] + orphans,
                use_sudo=True,
                capture=True,
                env=C_LOCALE_ENV,
            )
            if remove_res.ok:
                freed = parse_size_from_text(remove_res.stdout)
                print(f"  {OK} Removed {len(orphans)} orphaned {manager.label} packages")
                return freed, len(orphans), 1

    return 0, 0, 0


def clean_zombies(dry_run: bool = False) -> tuple[int, int, int]:
    """Identify and attempt to reap zombie processes."""
    # The state column is read as the English "Z" code.
    res = run_command(["ps", "-eo", "state,pid,ppid,comm"], capture=True, env=C_LOCALE_ENV)
    if not res.ok:
        return 0, 0, 0

    zombies = []
    for line in res.stdout.splitlines():
        if line.startswith("Z"):
            parts = line.split()
            if len(parts) >= 4:
                zombies.append({"pid": parts[1], "ppid": parts[2], "comm": parts[3]})

    if not zombies:
        return 0, 0, 0

    count = len(zombies)
    if dry_run:
        print(f"  {OK} {count} zombie processes detected")
        return 0, 0, 1

    parents = set(z["ppid"] for z in zombies)
    for ppid in parents:
        # Compare numerically, and only accept ASCII digits: a zero-padded "01"
        # or a Unicode digit form would slip past a string membership test and
        # send SIGCHLD to init (PID 1) or to the kernel's PID 0 placeholder.
        if not (ppid.isascii() and ppid.isdigit()):
            continue
        parent_pid = int(ppid)
        if parent_pid <= 1:
            continue
        run_command(["kill", "-SIGCHLD", str(parent_pid)], use_sudo=True, capture=True)

    print(f"  {OK} Signaled parents of {count} zombie processes")
    return 0, count, 1


def clean_old_kernels(dry_run: bool = False) -> tuple[int, int, int]:
    """Remove old kernel packages, keeping current and one previous version."""
    current_kernel = platform.release()
    # Asked from os-release rather than from PATH, like every other
    # package-manager decision: a Fedora box with the dpkg tools installed for
    # `alien` used to take the deb branch, find no linux-image-* rows, and return
    # without ever asking dnf about its kernels.
    manager = detect_package_manager(get_os_id())
    if manager is None:
        return 0, 0, 0
    tool = resolve_admin_tool(manager)
    if not shutil.which(tool):
        return 0, 0, 0

    if manager.key == "apt" and shutil.which("dpkg"):
        # Rows are matched on dpkg's English "ii" status pair.
        res = run_command(["dpkg", "-l", "linux-image-*"], capture=True, env=C_LOCALE_ENV)
        if not res.ok or not res.stdout:
            return 0, 0, 0
        installed = []
        for line in res.stdout.splitlines():
            if line.startswith("ii") and "linux-image-" in line:
                parts = line.split()
                if len(parts) >= 2:
                    pkg = parts[1]
                    is_meta = pkg.endswith("-generic") and not any(
                        c.isdigit() for c in pkg.split("-")[2:3]
                    )
                    if is_meta:
                        continue
                    installed.append(pkg)
        removable = [p for p in installed if current_kernel.split("-")[0] not in p]
        if len(removable) <= 1:
            return 0, 0, 0
        to_remove = removable[:-1]
        if dry_run:
            print(f"  {OK} {len(to_remove)} old kernel(s) would be removed")
            return 0, 0, 1
        for pkg in to_remove:
            run_command(
                [tool, "purge", "-y", pkg],
                use_sudo=True,
                capture=True,
                env=APT_NONINTERACTIVE_ENV,
            )
        print(f"  {OK} Removed {len(to_remove)} old kernel(s)")
        return 0, len(to_remove), 1

    elif manager.key == "dnf":
        res = run_command([tool, "repoquery", "--installonly", "--installed"], capture=True)
        if not res.ok or not res.stdout:
            return 0, 0, 0
        kernels = [k.strip() for k in res.stdout.splitlines() if k.strip()]
        removable = [k for k in kernels if current_kernel.split("-")[0] not in k]
        if len(removable) <= 1:
            return 0, 0, 0
        to_remove = removable[:-1]
        if dry_run:
            print(f"  {OK} {len(to_remove)} old kernel(s) would be removed")
            return 0, 0, 1
        for pkg in to_remove:
            run_command([tool, "remove", "-y", pkg], use_sudo=True, capture=True)
        print(f"  {OK} Removed {len(to_remove)} old kernel(s)")
        return 0, len(to_remove), 1

    return 0, 0, 0


def clean_rotated_logs(dry_run: bool = False) -> tuple[int, int, int]:
    """Remove rotated and compressed log files from /var/log."""
    total_size = 0
    total_items = 0
    log_dir = Path("/var/log")
    if not log_dir.exists():
        return 0, 0, 0
    rotated_suffixes = {".gz", ".xz", ".bz2", ".zst", ".old", ".1", ".2", ".3", ".4", ".5"}
    try:
        for item in log_dir.rglob("*"):
            if not item.is_file():
                continue
            if item.suffix in rotated_suffixes:
                size = get_size_fast(item)
                if dry_run:
                    total_size += size
                    total_items += 1
                else:
                    if safe_remove(item, use_trash=False)[0]:
                        total_size += size
                        total_items += 1
    except PermissionError:
        pass

    return DryRunReporter.report(
        "Rotated log files", freed_bytes=total_size, items_count=total_items, dry_run=dry_run
    )


def clean_system_data(dry_run: bool = False) -> tuple[int, int, int]:
    """Combined system and package-manager cleanup.

    The order is the one runner.py used to spell out task by task: package
    caches first, then what they leave behind, then logs, then processes.

    Each sub-cleaner prints its own line, so aggregating here changes the
    summary rather than the transcript -- the six tasks become one row, the
    granularity the other three groups already report at.
    """
    total_size = 0
    total_items = 0
    categories = 0

    for s, i, c in (
        clean_package_manager(dry_run),
        clean_orphaned_packages(dry_run),
        clean_old_kernels(dry_run),
        clean_journal(dry_run),
        clean_rotated_logs(dry_run),
        clean_zombies(dry_run),
    ):
        total_size += s
        total_items += i
        categories += c

    return total_size, total_items, categories
