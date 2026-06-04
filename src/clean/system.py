import shutil

from src.core.file_ops import bytes_to_human, parse_size_from_text
from src.core.system import get_os_id, run_command


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
