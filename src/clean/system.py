import platform
import shutil
from pathlib import Path

from ..core.constants import OK
from ..core.file_ops import bytes_to_human, get_size_fast, parse_size_from_text, safe_remove
from ..core.heavy_cache import get_package_manager_cleaner
from ..core.system import get_os_id, is_os_family, run_command


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

    res = run_command(["snap", "list", "--all"], capture=True)
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
    """Determines the physical cache directory paths for a package manager."""
    if cleaner_key == "dnf":
        for p in (
            Path("/var/cache/libdnf5"),
            Path("/var/cache/dnf5daemon-server"),
            Path("/var/cache/dnf"),
        ):
            if p.exists():
                return [p]
        return []
    elif cleaner_key == "apt":
        paths = [Path("/var/cache/apt/archives")]
        partial_path = Path("/var/cache/apt/archives/partial")
        if partial_path.exists():
            paths.append(partial_path)
        return paths
    elif cleaner_key == "pacman":
        pkg_path = Path("/var/cache/pacman/pkg")
        return [pkg_path] if pkg_path.exists() else []
    return []


def _measure_package_cache_size(cache_paths: list[Path]) -> int:
    """Measures total bytes stored in package manager cache directories."""
    return sum(get_size_fast(p) for p in cache_paths if p.exists())


def clean_package_manager(dry_run: bool = False) -> tuple[int, int, int]:
    """Clean system package manager caches."""
    freed = 0
    snap_items = 0
    snap_cats = 0
    os_id = get_os_id()
    cleaner = get_package_manager_cleaner(os_id)

    if cleaner and cleaner.key == "apt" and shutil.which(cleaner.executable):
        s, snap_items, snap_cats = clean_snaps(dry_run=dry_run)
        freed += s

    if not cleaner or not shutil.which(cleaner.executable):
        return freed, snap_items, snap_cats

    cache_paths = _get_package_manager_cache_paths(cleaner.key)
    pre_size = _measure_package_cache_size(cache_paths)

    if dry_run:
        size_hint = f" ({bytes_to_human(pre_size)})" if pre_size > 0 else ""
        print(f"  {OK} {cleaner.label}{size_hint} would be cleaned")
        return freed + pre_size, snap_items, snap_cats + 1

    cmd = list(cleaner.command)
    if cleaner.key == "dnf" and shutil.which("dnf5"):
        cmd = ["dnf5", "clean", "packages"]

    res = run_command(cmd, use_sudo=True, capture=True)
    post_size = _measure_package_cache_size(cache_paths)
    measured_freed = max(0, pre_size - post_size)

    if measured_freed > 0:
        freed += measured_freed
    elif res.ok and res.stdout:
        freed += parse_size_from_text(res.stdout)

    if res.ok:
        freed_str = f" ({bytes_to_human(freed)})" if freed > 0 else ""
        print(f"  {OK} Cleaned {cleaner.label}{freed_str}")
        return freed, snap_items + 1, snap_cats + 1

    return freed, snap_items, snap_cats


def clean_journal(dry_run: bool = False) -> tuple[int, int, int]:
    """Vacuum systemd journal logs."""
    if not shutil.which("journalctl"):
        return 0, 0, 0

    if dry_run:
        print(f"  {OK} journal logs would be vacuumed")
        return 0, 0, 1

    res = run_command(["journalctl", "--vacuum-size=1M"], use_sudo=True, capture=True)
    if res.ok and res.stdout:
        freed = parse_size_from_text(res.stdout)
        if freed > 0:
            print(f"  {OK} Vacuumed journal logs ({bytes_to_human(freed)})")
            return freed, 1, 1
    return 0, 0, 0


def clean_orphaned_packages(dry_run: bool = False) -> tuple[int, int, int]:
    """Remove orphaned dependencies that are no longer needed."""
    os_id = get_os_id()
    freed = 0

    if (
        os_id in ("ubuntu", "debian", "linuxmint", "pop", "elementary") or is_os_family("debian")
    ) and shutil.which("apt-get"):
        if dry_run:
            print(f"  {OK} Orphaned APT packages would be autoremoved")
            return 0, 0, 1
        res = run_command(["apt-get", "autoremove", "-y"], use_sudo=True, capture=True)
        if res.ok:
            freed = parse_size_from_text(res.stdout)
            print(f"  {OK} Removed orphaned APT packages")
            return freed, 1, 1

    elif (
        os_id in ("fedora", "rhel", "centos", "rocky", "almalinux") or is_os_family("fedora")
    ) and (shutil.which("dnf5") or shutil.which("dnf")):
        dnf_cmd = "dnf5" if shutil.which("dnf5") else "dnf"
        if dry_run:
            print(f"  {OK} Orphaned DNF packages would be autoremoved")
            return 0, 0, 1
        res = run_command([dnf_cmd, "autoremove", "-y"], use_sudo=True, capture=True)
        if res.ok:
            freed = parse_size_from_text(res.stdout)
            items = res.stdout.count("\n") // 2
            print(f"  {OK} Removed orphaned DNF packages ({bytes_to_human(freed)})")
            return freed, items, 1

    elif shutil.which("pacman"):
        list_res = run_command(["pacman", "-Qtdq"], capture=True)
        if list_res.ok and list_res.stdout.strip():
            orphans = list_res.stdout.split()
            if dry_run:
                print(f"  {OK} {len(orphans)} orphaned Pacman packages would be removed")
                return 0, 0, 1
            remove_res = run_command(
                ["pacman", "-Rns", "--noconfirm"] + orphans, use_sudo=True, capture=True
            )
            if remove_res.ok:
                freed = parse_size_from_text(remove_res.stdout)
                print(f"  {OK} Removed {len(orphans)} orphaned Pacman packages")
                return freed, len(orphans), 1

    return 0, 0, 0


def clean_zombies(dry_run: bool = False) -> tuple[int, int, int]:
    """Identify and attempt to reap zombie processes."""
    res = run_command(["ps", "-eo", "state,pid,ppid,comm"], capture=True)
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

    if shutil.which("dpkg") and shutil.which("apt-get"):
        res = run_command(["dpkg", "-l", "linux-image-*"], capture=True)
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
            run_command(["apt-get", "purge", "-y", pkg], use_sudo=True, capture=True)
        print(f"  {OK} Removed {len(to_remove)} old kernel(s)")
        return 0, len(to_remove), 1

    elif shutil.which("dnf5") or shutil.which("dnf"):
        dnf_cmd = "dnf5" if shutil.which("dnf5") else "dnf"
        res = run_command([dnf_cmd, "repoquery", "--installonly", "--installed"], capture=True)
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
            run_command([dnf_cmd, "remove", "-y", pkg], use_sudo=True, capture=True)
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
