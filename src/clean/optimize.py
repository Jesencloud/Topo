import contextlib
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
from ..core.constants import BOLD, GRAY, GREEN, PURPLE, RED, RESET, YELLOW
from ..core.file_ops import bytes_to_human, get_size, parse_size_from_text, safe_remove
from ..core.system import has_sudo, run_command

# Lock to ensure parallel tasks don't corrupt the terminal output
print_lock = threading.Lock()
SQLITE_MAX_OPTIMIZE_SIZE = 100 * 1024 * 1024
SQLITE_MIN_FREE_BYTES = 5 * 1024 * 1024
SQLITE_MIN_FREE_RATIO = 0.10
SQLITE_VACUUM_TIMEOUT = 20
MEMORY_PRESSURE_AVAILABLE_RATIO = 0.15


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


def _is_any_process_running(process_names: list[str]) -> bool:
    if not shutil.which("pgrep"):
        return False
    return any(
        run_command(["pgrep", "-x", name], capture=True, timeout=1).ok for name in process_names
    )


def _is_sqlite_database(db_file: Path) -> bool:
    try:
        with db_file.open("rb") as f:
            return f.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def _set_sqlite_timeout(conn: sqlite3.Connection, deadline: float) -> None:
    def abort_if_expired():
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(abort_if_expired, 10000)


def vacuum_single_db(db_file):
    """Worker function to vacuum a single database only if worth it."""
    db_path = Path(db_file)
    if db_path.name.endswith(("-wal", "-shm")):
        return 0
    if not _is_sqlite_database(db_path):
        return 0
    try:
        if db_path.stat().st_size > SQLITE_MAX_OPTIMIZE_SIZE:
            return 0
    except OSError:
        return 0

    try:
        with contextlib.closing(sqlite3.connect(db_path, timeout=1)) as conn:
            _set_sqlite_timeout(conn, time.monotonic() + SQLITE_VACUUM_TIMEOUT)
            cursor = conn.cursor()
            cursor.execute("PRAGMA integrity_check")
            if cursor.fetchone()[0] != "ok":
                return 0
            cursor.execute("PRAGMA page_count")
            page_count = cursor.fetchone()[0]
            cursor.execute("PRAGMA freelist_count")
            freelist_count = cursor.fetchone()[0]
            cursor.execute("PRAGMA page_size")
            page_size = cursor.fetchone()[0]

            if page_count == 0:
                return 0

            free_ratio = freelist_count / page_count
            free_bytes = freelist_count * page_size
            if free_ratio <= SQLITE_MIN_FREE_RATIO and free_bytes <= SQLITE_MIN_FREE_BYTES:
                return 0

            old_size = get_size(db_path)
            conn.execute("VACUUM")
        # Connection now closed via closing(); the on-disk size has settled.
        return old_size - get_size(db_path)
    except (OSError, sqlite3.Error):
        return 0


def run_vacuum_all(dry_run=False):
    """Task to optimize all browser databases."""
    targets = [
        ("Firefox", ["firefox"], "~/.mozilla/firefox/*/places.sqlite"),
        ("Firefox", ["firefox"], "~/.mozilla/firefox/*/cookies.sqlite"),
        (
            "Chrome",
            ["google-chrome", "chrome", "chromium"],
            "~/.config/google-chrome/Default/History",
        ),
        (
            "Brave",
            ["brave", "brave-browser"],
            "~/.config/BraveSoftware/Brave-Browser/Default/History",
        ),
        ("Edge", ["microsoft-edge"], "~/.config/microsoft-edge/Default/History"),
    ]

    db_files = []
    busy_apps = set()
    for app_name, process_names, pattern in targets:
        if _is_any_process_running(process_names):
            busy_apps.add(app_name)
            continue
        path_obj = Path(pattern).expanduser()
        parent = path_obj.parent
        if not parent.exists():
            continue
        for f in parent.glob(path_obj.name):
            if f.is_file():
                db_files.append(f)

    if busy_apps and not db_files:
        return f"{', '.join(sorted(busy_apps))} running; database optimization skipped"
    if not db_files:
        return None
    if dry_run:
        suffix = f"; skipped running app(s): {', '.join(sorted(busy_apps))}" if busy_apps else ""
        return f"Found {len(db_files)} database(s) to optimize{suffix}"

    total_saved = 0
    # Nested pool or just direct execution since we are already in a pool
    for db in db_files:
        total_saved += vacuum_single_db(db)

    saved_str = f" (compressed {bytes_to_human(total_saved)})" if total_saved > 0 else ""
    suffix = f"; skipped running app(s): {', '.join(sorted(busy_apps))}" if busy_apps else ""
    return f"Optimized {len(db_files)} browser database(s){saved_str}{suffix}"


def run_fstrim(dry_run=False):
    if not shutil.which("fstrim"):
        return None
    if dry_run:
        return "SSD partitions would be trimmed (fstrim)"
    if run_command(["fstrim", "-av"], use_sudo=True, capture=True).ok:
        return "SSD partitions trimmed (fstrim)"
    return None


def run_fccache(dry_run=False):
    if not shutil.which("fc-cache"):
        return None
    if dry_run:
        return "System font cache would be refreshed"
    if run_command(["fc-cache"], capture=True).ok:
        return "System font cache refreshed"
    return None


def run_dns_flush(dry_run=False):
    dns_cmd = None
    if shutil.which("resolvectl"):
        dns_cmd = ["resolvectl", "flush-caches"]
    elif shutil.which("nscd"):
        dns_cmd = ["nscd", "-i", "hosts"]
    if not dns_cmd:
        return None
    if dry_run:
        return "DNS resolver cache would be flushed"
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
        if shutil.which("systemctl"):
            run_command(["systemctl", "--user", "daemon-reload"], capture=True, timeout=10)
        return f"Removed {removed} broken user systemd service(s)"

    return f"Found {len(broken_units)} broken user systemd service(s)"


def run_swap_management(dry_run=False):
    """Reset swap if RAM is plentiful to reduce micro-stutter."""
    if not shutil.which("swapoff") or not shutil.which("swapon"):
        return None

    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split(":")
                if len(parts) == 2:
                    key = parts[0].strip()
                    raw_val = parts[1].strip().split()[0]
                    mem[key] = int(raw_val) * 1024

        available = mem.get("MemAvailable", 0)
        total_swap = mem.get("SwapTotal", 0)
        free_swap = mem.get("SwapFree", 0)
        used_swap = total_swap - free_swap

        if used_swap <= 0:
            return None

        # Only reset if we have 2x the used swap available in RAM for safety
        if available > used_swap * 2:
            if dry_run:
                return f"Swap would be reset (Currently using {bytes_to_human(used_swap)})"

            # swapoff -a can take time as data is moved back to RAM
            if run_command(["swapoff", "-a"], use_sudo=True, timeout=120).ok:
                run_command(["swapon", "-a"], use_sudo=True, timeout=30)
                return f"Swap reset successful (Reclaimed {bytes_to_human(used_swap)})"
    except (OSError, ValueError):
        pass
    return None


def run_journal_optimization(dry_run=False):
    """Aggressive journal vacuuming (keep 3 days)."""
    if not shutil.which("journalctl"):
        return None
    if dry_run:
        return "System journal would be vacuumed to 3 days"

    res = run_command(["journalctl", "--vacuum-time=3d"], use_sudo=True, capture=True)
    if res.ok and res.stdout:
        freed = parse_size_from_text(res.stdout)
        if freed > 0:
            return f"Journal vacuumed to 3 days (Reclaimed {bytes_to_human(freed)})"
    return "Journal already optimized (under 3 days)"


def run_coredump_cleanup(dry_run=False):
    """Clean system coredump files."""
    coredump_dir = Path("/var/lib/systemd/coredump")
    if not coredump_dir.exists() and not shutil.which("journalctl"):
        return None

    if dry_run:
        return "System coredumps would be cleared"

    res = run_command(["journalctl", "--vacuum-coredump=0"], use_sudo=True, capture=True)
    if res.ok:
        return "System coredumps cleared"
    return None


def run_broken_symlink_cleanup(dry_run=False):
    """Remove broken symlinks in common user directories."""
    search_dirs = [
        Path.home() / ".local/bin",
        Path.home() / "Desktop",
        Path.home() / "Documents",
    ]

    broken = []
    for d in search_dirs:
        if not d.exists():
            continue
        try:
            for item in d.iterdir():
                if item.is_symlink() and not item.exists():
                    broken.append(item)
        except OSError:
            continue

    if not broken:
        return None

    if dry_run:
        return f"Found {len(broken)} broken user symlinks"

    removed = 0
    for link in broken:
        try:
            link.unlink()
            removed += 1
        except OSError:
            continue

    if removed > 0:
        return f"Removed {removed} broken user symlink(s)"
    return None


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


def _read_memory_pressure() -> tuple[bool, str]:
    try:
        values = {}
        with open("/proc/meminfo") as f:
            for line in f:
                key, raw_value = line.split(":", 1)
                if key in {"MemTotal", "MemAvailable"}:
                    values[key] = int(raw_value.strip().split()[0]) * 1024
        total = values.get("MemTotal", 0)
        available = values.get("MemAvailable", 0)
        if total <= 0 or available <= 0:
            return False, ""
        ratio = available / total
        return ratio < MEMORY_PRESSURE_AVAILABLE_RATIO, f"{ratio * 100:.0f}% memory available"
    except (OSError, ValueError, IndexError):
        return False, ""


def run_memory_opt(dry_run=False):
    pressure_high, detail = _read_memory_pressure()
    if not pressure_high:
        return (
            f"Memory pressure already optimal ({detail})"
            if detail
            else "Memory pressure already optimal"
        )
    if dry_run:
        return "PageCache would be released (Memory pressure high)"
    if not has_sudo():
        return None
    run_command(["sync"], capture=True)
    if run_command(
        ["bash", "-c", "echo 1 > /proc/sys/vm/drop_caches"], use_sudo=True, capture=True
    ).ok:
        return "PageCache released (Memory relief)"
    return None


def run_desktop_database_refresh(dry_run=False):
    app_dir = Path.home() / ".local/share/applications"
    if not app_dir.exists() or not shutil.which("update-desktop-database"):
        return None
    if dry_run:
        return "Desktop application database would be refreshed"
    if run_command(["update-desktop-database", str(app_dir)], capture=True, timeout=30).ok:
        return "Desktop application database refreshed"
    return None


def run_mime_database_refresh(dry_run=False):
    mime_dir = Path.home() / ".local/share/mime"
    if not mime_dir.exists() or not shutil.which("update-mime-database"):
        return None
    if dry_run:
        return "MIME database would be refreshed"
    if run_command(["update-mime-database", str(mime_dir)], capture=True, timeout=30).ok:
        return "MIME database refreshed"
    return None


def run_thumbnail_cleanup(dry_run=False):
    thumb_cache = os.path.expanduser("~/.cache/thumbnails")
    if os.path.exists(thumb_cache):
        if dry_run:
            return "Desktop thumbnail cache would be cleared"
        shutil.rmtree(thumb_cache, ignore_errors=True)
        return "Desktop thumbnail cache cleared"
    return None


def optimize_system(dry_run=False):
    os.system("clear")
    print(f"\n{PURPLE}System Optimization{RESET}\n")
    print(f"{GRAY}Running maintenance tasks in parallel...{RESET}")

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
            lambda: run_fstrim(dry_run),
            lambda: run_fccache(dry_run),
            lambda: run_dns_flush(dry_run),
            lambda: run_memory_opt(dry_run),
            lambda: run_swap_management(dry_run),
            lambda: run_journal_optimization(dry_run),
            lambda: run_coredump_cleanup(dry_run),
            lambda: run_broken_symlink_cleanup(dry_run),
            lambda: run_thumbnail_cleanup(dry_run),
            lambda: run_desktop_database_refresh(dry_run),
            lambda: run_mime_database_refresh(dry_run),
            lambda: run_vacuum_all(dry_run),
            lambda: run_zombie_cleanup(dry_run),
            lambda: run_systemd_user_service_cleanup(dry_run),
        ]
    else:
        tasks = [
            lambda: run_fstrim(True),
            lambda: run_fccache(True),
            lambda: run_dns_flush(True),
            lambda: run_memory_opt(True),
            lambda: run_swap_management(True),
            lambda: run_journal_optimization(True),
            lambda: run_coredump_cleanup(True),
            lambda: run_broken_symlink_cleanup(True),
            lambda: run_thumbnail_cleanup(True),
            lambda: run_vacuum_all(True),
            lambda: run_desktop_database_refresh(True),
            lambda: run_mime_database_refresh(True),
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
