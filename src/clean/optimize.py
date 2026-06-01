import os
import shlex
import shutil
import sqlite3
import sys
import termios
import threading
import time
import tty
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from ..core import system
from ..core.constants import BLUE, BOLD, GRAY, GREEN, PURPLE, RED, RESET, YELLOW
from ..core.file_ops import bytes_to_human, get_size, safe_remove
from ..core.system import has_sudo, run_command

# Lock to ensure parallel tasks don't corrupt the terminal output
print_lock = threading.Lock()


def opt_log(message, success=True, skipped=False):
    if skipped:
        icon = f"{GRAY}◎{RESET}"
        msg = f"{GRAY}{message} · skipped{RESET}"
    else:
        icon = f"{GREEN}✓{RESET}"
        msg = f"{message}"

    with print_lock:
        # Use a single print statement within a lock to ensure atomicity
        print(f"  {icon} {msg}")


def _read_sudo_choice() -> str:
    if not sys.stdin.isatty():
        return "\n"

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def vacuum_single_db(db_file):
    """Worker function to vacuum a single database only if worth it."""
    try:
        conn = sqlite3.connect(db_file, timeout=1)
        cursor = conn.cursor()
        cursor.execute("PRAGMA page_count")
        page_count = cursor.fetchone()[0]
        cursor.execute("PRAGMA freelist_count")
        freelist_count = cursor.fetchone()[0]
        cursor.execute("PRAGMA page_size")
        page_size = cursor.fetchone()[0]

        if page_count == 0:
            conn.close()
            return 0

        free_ratio = freelist_count / page_count
        free_bytes = freelist_count * page_size

        if free_ratio > 0.1 or free_bytes > 5 * 1024 * 1024:
            old_size = get_size(db_file)
            conn.execute("VACUUM")
            conn.close()
            return old_size - get_size(db_file)

        conn.close()
        return 0
    except Exception:
        return 0


def run_vacuum_all(dry_run=False):
    """Task to optimize all browser databases."""
    targets = [
        "~/.mozilla/firefox/*/places.sqlite",
        "~/.mozilla/firefox/*/cookies.sqlite",
        "~/.config/google-chrome/Default/History",
        "~/.config/BraveSoftware/Brave-Browser/Default/History",
        "~/.config/microsoft-edge/Default/History",
    ]

    db_files = []
    for pattern in targets:
        path_obj = Path(pattern).expanduser()
        parent = path_obj.parent
        if not parent.exists():
            continue
        for f in parent.glob(path_obj.name):
            if f.is_file():
                db_files.append(f)

    if not db_files:
        return None
    if dry_run:
        return f"Found {len(db_files)} database(s) to optimize"

    total_saved = 0
    # Nested pool or just direct execution since we are already in a pool
    for db in db_files:
        total_saved += vacuum_single_db(db)

    saved_str = f" (compressed {bytes_to_human(total_saved)})" if total_saved > 0 else ""
    return f"Optimized {len(db_files)} browser database(s){saved_str}"


def run_fstrim():
    if not shutil.which("fstrim"):
        return None
    if run_command(["fstrim", "-av"], use_sudo=True, capture=True).ok:
        return "SSD partitions trimmed (fstrim)"
    return None


def run_fccache():
    if not shutil.which("fc-cache"):
        return None
    if run_command(["fc-cache"], capture=True).ok:
        return "System font cache refreshed"
    return None


def run_dns_flush():
    dns_cmd = None
    if shutil.which("resolvectl"):
        dns_cmd = ["resolvectl", "flush-caches"]
    elif shutil.which("nscd"):
        dns_cmd = ["nscd", "-i", "hosts"]
    if not dns_cmd:
        return None
    if run_command(dns_cmd, use_sudo=True, capture=True).ok:
        return "DNS resolver cache flushed"
    return None


def run_zombie_cleanup(dry_run=False):
    autostart_dir = Path.home() / ".config" / "autostart"
    if not autostart_dir.exists():
        return None
    zombies = 0
    for desktop_file in autostart_dir.glob("*.desktop"):
        try:
            is_zombie = False
            with open(desktop_file) as f:
                for line in f:
                    if line.startswith("Exec="):
                        line_content = line.split("=", 1)[1].strip()
                        if not line_content:
                            continue
                        cmd = line_content.split()[0]
                        if (
                            cmd.startswith("/")
                            and not os.path.exists(cmd)
                            or not cmd.startswith("/")
                            and not shutil.which(cmd)
                        ):
                            is_zombie = True
                        break
            if is_zombie:
                if not dry_run:
                    desktop_file.unlink()
                zombies += 1
        except Exception:
            continue
    if zombies > 0:
        return f"Removed {zombies} zombie autostart entries"
    return None


def run_systemd_user_service_cleanup(dry_run=False):
    """Remove user service units whose ExecStart target no longer exists."""
    user_systemd_dir = Path.home() / ".config" / "systemd" / "user"
    if not user_systemd_dir.exists():
        return None

    broken_units = []
    for service_file in user_systemd_dir.glob("*.service"):
        try:
            exec_target = _extract_service_exec_target(service_file)
        except OSError:
            continue
        if exec_target and not _service_exec_target_exists(exec_target):
            broken_units.append(service_file)

    if not broken_units:
        return None

    if not dry_run:
        removed = 0
        for service_file in broken_units:
            if safe_remove(service_file, use_trash=False)[0]:
                removed += 1
        if removed == 0:
            return None
        return f"Removed {removed} broken user systemd service(s)"

    return f"Found {len(broken_units)} broken user systemd service(s)"


def _extract_service_exec_target(service_file: Path) -> str:
    for line in service_file.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("ExecStart="):
            continue
        value = line.split("=", 1)[1].strip()
        if not value:
            return ""
        try:
            parts = shlex.split(value)
        except ValueError:
            parts = value.split()
        if not parts:
            return ""
        command = parts[0].lstrip("-@+!")
        return command
    return ""


def _service_exec_target_exists(command: str) -> bool:
    if command.startswith("/"):
        return Path(command).exists()
    return shutil.which(command) is not None


def run_memory_opt():
    if not has_sudo():
        return None
    run_command(["sync"], capture=True)
    if run_command(["bash", "-c", "echo 1 > /proc/sys/vm/drop_caches"], use_sudo=True, capture=True).ok:
        return "PageCache released (Memory relief)"
    return None


def run_thumbnail_cleanup():
    thumb_cache = os.path.expanduser("~/.cache/thumbnails")
    if os.path.exists(thumb_cache):
        shutil.rmtree(thumb_cache, ignore_errors=True)
        return "Desktop thumbnail cache cleared"
    return None


def optimize_system(dry_run=False):
    os.system("clear")
    print(f"{BLUE}{BOLD}System Optimization{RESET}")
    print(f"{GRAY}Running maintenance tasks in parallel...{RESET}\n")

    if not dry_run:
        print(
            f"{PURPLE}➔{RESET} Optimization tasks need sudo. "
            f"{GREEN}Enter{RESET} continue, {GRAY}Space{RESET} skip:",
            end=" ",
            flush=True,
        )
        choice = _read_sudo_choice()
        print()
        if choice in (" ", "\x1b"):
            return False
        if not system.ensure_sudo_session(
            f"{PURPLE}➔{RESET} System optimization requires admin access\n"
            f"{PURPLE}➔{RESET} Password: "
        ):
            if system.SUDO_CANCELLED:
                print(f" {YELLOW}⚠️  Optimization cancelled by user.{RESET}\n")
            else:
                print(f" {RED}✗{RESET} Authorization failed. Optimization skipped.\n")
            return
        print(f" {GREEN}✓{RESET} Authorization successful.\n")

    start_time = time.time()

    tasks = []
    if not dry_run:
        tasks = [
            run_fstrim,
            run_fccache,
            run_dns_flush,
            run_memory_opt,
            run_thumbnail_cleanup,
            lambda: run_vacuum_all(dry_run),
            lambda: run_zombie_cleanup(dry_run),
            lambda: run_systemd_user_service_cleanup(dry_run),
        ]
    else:
        tasks = [
            lambda: run_vacuum_all(True),
            lambda: run_zombie_cleanup(True),
            lambda: run_systemd_user_service_cleanup(True),
        ]

    with ThreadPoolExecutor(max_workers=len(tasks)) as executor:
        futures = {executor.submit(task): task for task in tasks}

        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    opt_log(result, skipped=dry_run)
            except Exception:
                # Silently skip failed maintenance tasks
                pass

    duration = time.time() - start_time
    print(f"\n{GREEN}{BOLD}✨ All tasks completed in {duration:.1f}s.{RESET}")
