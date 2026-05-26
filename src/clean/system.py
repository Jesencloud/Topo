import shutil
import re
from src.core.system import get_os_id, run_command
from src.core.file_ops import bytes_to_human

def parse_size_from_text(text):
    """Simple parser for sizes like '1.2M', '500B', '2.5 GB' in command output."""
    if not text: return 0
    # Common patterns: 'freed 1.2M', 'total of 500 B', 'reclaimed 2.5 GB'
    match = re.search(r'([0-9.]+)\s*([KMGTB]?B|[KMG])', text, re.IGNORECASE)
    if match:
        val = float(match.group(1))
        unit = match.group(2).upper()
        if 'G' in unit: val *= 1024 * 1024 * 1024
        elif 'M' in unit: val *= 1024 * 1024
        elif 'K' in unit: val *= 1024
        return int(val)
    return 0

def clean_package_manager(dry_run=False):
    freed = 0
    os_id = get_os_id()
    cmd = []
    description = ""
    
    if os_id in ("fedora", "rhel", "centos") and shutil.which("dnf"):
        cmd = ["dnf", "clean", "all"]
        description = "DNF cache"
    elif os_id in ("ubuntu", "debian") and shutil.which("apt-get"):
        cmd = ["apt-get", "clean"] # apt-get clean doesn't report size easily
        description = "APT cache"
    elif os_id == "arch" and shutil.which("pacman"):
        cmd = ["pacman", "-Sc", "--noconfirm"]
        description = "Pacman cache"

    if not cmd:
        return 0, 0, 0

    if dry_run:
        # In dry-run, we don't know the exact size easily for system tools without running them
        # We just report that we would check it
        print(f"  \033[0;32m✓\033[0m {description} would be cleaned")
        return 0, 0, 1

    res = run_command(cmd, use_sudo=True, capture=True)
    if res and res.stdout:
        freed = parse_size_from_text(res.stdout)
        if freed > 0:
            print(f"  \033[0;32m✓\033[0m Cleaned {description} ({bytes_to_human(freed)})")
            return freed, 1, 1
    
    return 0, 0, 0

def clean_journal(dry_run=False):
    if shutil.which("journalctl"):
        if dry_run:
            print(f"  \033[0;32m✓\033[0m journal logs would be vacuumed")
            return 0, 0, 1
            
        res = run_command(["journalctl", "--vacuum-time=7d"], use_sudo=True, capture=True)
        if res and res.stdout:
            freed = parse_size_from_text(res.stdout)
            if freed > 0:
                print(f"  \033[0;32m✓\033[0m Vacuumed journal logs ({bytes_to_human(freed)})")
                return freed, 1, 1
    return 0, 0, 0

