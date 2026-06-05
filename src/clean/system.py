import shutil

from ..core.file_ops import bytes_to_human, parse_size_from_text
from ..core.system import get_os_id, run_command


def clean_snaps(dry_run=False):
    """Removes old revisions of snaps to save massive space on Ubuntu."""
    if shutil.which("snap"):
        if dry_run:
            print("  \033[0;32m✓\033[0m Old Snap revisions would be removed")
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
            print(f"  \033[0;32m✓\033[0m Removed {count} old Snap revisions")
            return 0, count, 1
    return 0, 0, 0


def clean_package_manager(dry_run=False):
    """Clean system package manager caches."""
    freed = 0
    snap_items = 0
    snap_cats = 0
    os_id = get_os_id()
    cmd = []
    desc = ""

    if os_id in ("fedora", "rhel", "centos") and shutil.which("dnf"):
        cmd = ["dnf", "clean", "all"]
        desc = "DNF cache"
    elif os_id in ("ubuntu", "debian") and shutil.which("apt-get"):
        cmd = ["apt-get", "clean"]
        desc = "APT cache"
        # Old Snap revisions are a separate cleanup category; keep their stats.
        s, snap_items, snap_cats = clean_snaps(dry_run=dry_run)
        freed += s
    elif os_id == "arch" and shutil.which("pacman"):
        cmd = ["pacman", "-Sc", "--noconfirm"]
        desc = "Pacman cache"

    if not cmd:
        return freed, snap_items, snap_cats

    if dry_run:
        print(f"  \033[0;32m✓\033[0m {desc} would be cleaned")
        return freed, snap_items, snap_cats + 1

    res = run_command(cmd, use_sudo=True, capture=True)
    if res.ok and res.stdout:
        freed += parse_size_from_text(res.stdout)
        print(f"  \033[0;32m✓\033[0m Cleaned {desc} ({bytes_to_human(freed)})")
        return freed, snap_items + 1, snap_cats + 1

    if res.ok and desc == "APT cache":  # apt-get clean is silent
        print(f"  \033[0;32m✓\033[0m Cleaned {desc}")
        return freed, snap_items + 1, snap_cats + 1

    return freed, snap_items, snap_cats


def clean_journal(dry_run=False):
    """Vacuum systemd journal logs."""
    if shutil.which("journalctl"):
        if dry_run:
            print("  \033[0;32m✓\033[0m journal logs would be vacuumed")
            return 0, 0, 1

        res = run_command(["journalctl", "--vacuum-size=1M"], use_sudo=True, capture=True)
        if res.ok and res.stdout:
            freed = parse_size_from_text(res.stdout)
            if freed > 0:
                print(f"  \033[0;32m✓\033[0m Vacuumed journal logs ({bytes_to_human(freed)})")
                return freed, 1, 1
    return 0, 0, 0


def clean_orphaned_packages(dry_run=False):
    """Remove orphaned dependencies that are no longer needed."""
    os_id = get_os_id()
    freed = 0
    items = 0

    if os_id in ("fedora", "rhel", "centos") and shutil.which("dnf"):
        if dry_run:
            print("  \033[0;32m✓\033[0m Orphaned DNF packages would be autoremoved")
            return 0, 0, 1
        res = run_command(["dnf", "autoremove", "-y"], use_sudo=True, capture=True)
        if res.ok:
            freed = parse_size_from_text(res.stdout)
            # DNF autoremove output usually lists packages. We can estimate count.
            items = res.stdout.count("\n") // 2  # Rough estimate
            print(f"  \033[0;32m✓\033[0m Removed orphaned DNF packages ({bytes_to_human(freed)})")
            return freed, items, 1

    elif os_id in ("ubuntu", "debian") and shutil.which("apt-get"):
        if dry_run:
            print("  \033[0;32m✓\033[0m Orphaned APT packages would be autoremoved")
            return 0, 0, 1
        res = run_command(["apt-get", "autoremove", "-y"], use_sudo=True, capture=True)
        if res.ok:
            freed = parse_size_from_text(res.stdout)
            print("  \033[0;32m✓\033[0m Removed orphaned APT packages")
            return freed, 1, 1

    elif os_id == "arch" and shutil.which("pacman"):
        # List orphans
        list_res = run_command(["pacman", "-Qtdq"], capture=True)
        if list_res.ok and list_res.stdout.strip():
            orphans = list_res.stdout.split()
            if dry_run:
                print(
                    f"  \033[0;32m✓\033[0m {len(orphans)} orphaned Pacman packages would be removed"
                )
                return 0, 0, 1
            # Remove them
            remove_res = run_command(
                ["pacman", "-Rns", "--noconfirm"] + orphans, use_sudo=True, capture=True
            )
            if remove_res.ok:
                freed = parse_size_from_text(remove_res.stdout)
                print(f"  \033[0;32m✓\033[0m Removed {len(orphans)} orphaned Pacman packages")
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
        print(f"  \033[0;32m✓\033[0m {count} zombie processes detected")
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

    print(f"  \033[0;32m✓\033[0m Signaled parents of {count} zombie processes")
    return 0, count, 1
