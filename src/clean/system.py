import platform
import shutil
from pathlib import Path

from ..core.constants import OK
from ..core.file_ops import bytes_to_human, get_size_fast, parse_size_from_text, safe_remove
from ..core.heavy_cache import get_package_manager_cleaner
from ..core.system import get_os_id, run_command


def clean_snaps(dry_run=False):
    """Removes old revisions of snaps to save massive space on Ubuntu."""
    if shutil.which("snap"):
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
                    res = run_command(
                        ["snap", "remove", parts[0], "--revision", parts[2]],
                        use_sudo=True,
                        capture=True,
                    )
                    if res.ok:
                        count += 1

        if count > 0:
            print(f"  {OK} Removed {count} old Snap revisions")
            return 0, count, 1
    return 0, 0, 0


def clean_package_manager(dry_run=False):
    """Clean system package manager caches."""
    freed = 0
    snap_items = 0
    snap_cats = 0
    os_id = get_os_id()
    cleaner = get_package_manager_cleaner(os_id)

    if cleaner and cleaner.key == "apt" and shutil.which(cleaner.executable):
        # Old Snap revisions are a separate cleanup category; keep their stats.
        s, snap_items, snap_cats = clean_snaps(dry_run=dry_run)
        freed += s

    if not cleaner or not shutil.which(cleaner.executable):
        return freed, snap_items, snap_cats

    if dry_run:
        print(f"  {OK} {cleaner.label} would be cleaned")
        return freed, snap_items, snap_cats + 1

    res = run_command(list(cleaner.command), use_sudo=True, capture=True)
    if res.ok and res.stdout:
        freed += parse_size_from_text(res.stdout)
        print(f"  {OK} Cleaned {cleaner.label} ({bytes_to_human(freed)})")
        return freed, snap_items + 1, snap_cats + 1

    if res.ok and cleaner.key == "apt":  # apt-get clean is silent
        print(f"  {OK} Cleaned {cleaner.label}")
        return freed, snap_items + 1, snap_cats + 1

    return freed, snap_items, snap_cats


def clean_journal(dry_run=False):
    """Vacuum systemd journal logs."""
    if shutil.which("journalctl"):
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


def clean_orphaned_packages(dry_run=False):
    """Remove orphaned dependencies that are no longer needed."""
    os_id = get_os_id()
    freed = 0
    items = 0

    if os_id in ("fedora", "rhel", "centos") and shutil.which("dnf"):
        if dry_run:
            print(f"  {OK} Orphaned DNF packages would be autoremoved")
            return 0, 0, 1
        res = run_command(["dnf", "autoremove", "-y"], use_sudo=True, capture=True)
        if res.ok:
            freed = parse_size_from_text(res.stdout)
            # DNF autoremove output usually lists packages. We can estimate count.
            items = res.stdout.count("\n") // 2  # Rough estimate
            print(f"  {OK} Removed orphaned DNF packages ({bytes_to_human(freed)})")
            return freed, items, 1

    elif os_id in ("ubuntu", "debian") and shutil.which("apt-get"):
        if dry_run:
            print(f"  {OK} Orphaned APT packages would be autoremoved")
            return 0, 0, 1
        res = run_command(["apt-get", "autoremove", "-y"], use_sudo=True, capture=True)
        if res.ok:
            freed = parse_size_from_text(res.stdout)
            print(f"  {OK} Removed orphaned APT packages")
            return freed, 1, 1

    elif os_id == "arch" and shutil.which("pacman"):
        # List orphans
        list_res = run_command(["pacman", "-Qtdq"], capture=True)
        if list_res.ok and list_res.stdout.strip():
            orphans = list_res.stdout.split()
            if dry_run:
                print(f"  {OK} {len(orphans)} orphaned Pacman packages would be removed")
                return 0, 0, 1
            # Remove them
            remove_res = run_command(
                ["pacman", "-Rns", "--noconfirm"] + orphans, use_sudo=True, capture=True
            )
            if remove_res.ok:
                freed = parse_size_from_text(remove_res.stdout)
                print(f"  {OK} Removed {len(orphans)} orphaned Pacman packages")
                return freed, len(orphans), 1

    return 0, 0, 0


def clean_zombies(dry_run=False):
    """Identify and attempt to reap zombie processes."""
    # Find zombies: state 'Z'
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

    # Attempt to signal parents to reap zombies
    parents = set(z["ppid"] for z in zombies)
    reaped = 0
    for ppid in parents:
        if ppid == "1":
            continue  # Init will reap eventually
        # Send SIGCHLD to parent
        run_command(["kill", "-SIGCHLD", ppid], use_sudo=True, capture=True)
        reaped += 1

    print(f"  {OK} Signaled parents of {count} zombie processes")
    return 0, count, 1


def clean_old_kernels(dry_run=False):
    """Remove old kernel packages, keeping current and one previous version."""
    os_id = get_os_id()
    current_kernel = platform.release()

    if os_id in ("ubuntu", "debian") and shutil.which("dpkg"):
        res = run_command(["dpkg", "-l", "linux-image-*"], capture=True)
        if not res.ok or not res.stdout:
            return 0, 0, 0
        installed = []
        for line in res.stdout.splitlines():
            if line.startswith("ii") and "linux-image-" in line:
                parts = line.split()
                if len(parts) >= 2:
                    pkg = parts[1]
                    # Skip meta-packages like linux-image-generic
                    is_meta = pkg.endswith("-generic") and not any(
                        c.isdigit() for c in pkg.split("-")[2:3]
                    )
                    if is_meta:
                        continue
                    installed.append(pkg)
        # Filter out current kernel's package
        removable = [p for p in installed if current_kernel.split("-")[0] not in p]
        # Keep at most 1 old kernel (the most recent non-current)
        # Sort by version and remove all but the last
        if len(removable) <= 1:
            return 0, 0, 0
        to_remove = removable[:-1]  # Keep the latest removable one
        if dry_run:
            print(f"  {OK} {len(to_remove)} old kernel(s) would be removed")
            return 0, 0, 1
        for pkg in to_remove:
            run_command(["apt-get", "purge", "-y", pkg], use_sudo=True, capture=True)
        print(f"  {OK} Removed {len(to_remove)} old kernel(s)")
        return 0, len(to_remove), 1

    elif os_id in ("fedora", "rhel", "centos") and shutil.which("dnf"):
        # DNF respects installonly_limit but we can explicitly enforce keeping 2
        res = run_command(["dnf", "repoquery", "--installonly", "--installed"], capture=True)
        if not res.ok or not res.stdout:
            return 0, 0, 0
        kernels = [k.strip() for k in res.stdout.splitlines() if k.strip()]
        # Filter out current kernel
        removable = [k for k in kernels if current_kernel.split("-")[0] not in k]
        if len(removable) <= 1:
            return 0, 0, 0
        to_remove = removable[:-1]
        if dry_run:
            print(f"  {OK} {len(to_remove)} old kernel(s) would be removed")
            return 0, 0, 1
        for pkg in to_remove:
            run_command(["dnf", "remove", "-y", pkg], use_sudo=True, capture=True)
        print(f"  {OK} Removed {len(to_remove)} old kernel(s)")
        return 0, len(to_remove), 1

    return 0, 0, 0


def clean_rotated_logs(dry_run=False):
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
    if total_items > 0:
        status = "would be cleaned" if dry_run else "cleaned"
        print(f"  {OK} Rotated log files ({bytes_to_human(total_size)}) {status}")
        return total_size, total_items, 1
    return 0, 0, 0
