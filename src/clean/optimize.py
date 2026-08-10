import contextlib
import os
import shlex
import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..core import system, terminal_state
from ..core.constants import (
    BOLD,
    CLEAR_SCREEN,
    GRAY,
    GREEN,
    PURPLE,
    RED,
    RESET,
    SQLITE_PROGRESS_INTERVAL,
    YELLOW,
)
from ..core.desktop_entry import get_desktop_exec_command
from ..core.file_ops import (
    bytes_to_human,
    get_size,
    parse_size_from_text,
    safe_remove,
)
from ..core.system import has_sudo, run_command

# Lock to ensure parallel tasks don't corrupt the terminal output
print_lock = threading.Lock()
SQLITE_MAX_OPTIMIZE_SIZE = 100 * 1024 * 1024
SQLITE_MIN_FREE_BYTES = 5 * 1024 * 1024
SQLITE_MIN_FREE_RATIO = 0.10
SQLITE_VACUUM_TIMEOUT = 20
COREDUMP_DIR = Path("/var/lib/systemd/coredump")
_MIN_RAM_SWAP_RATIO = 2


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


_read_sudo_choice = terminal_state.read_sudo_choice


class OptimizationRegistry:
    """Registry for automatic discovery of system optimization tasks."""

    tasks: list[Any] = []

    @classmethod
    def register(cls, func: Any) -> Any:
        cls.tasks.append(func)
        return func


register_optimization_task = OptimizationRegistry.register


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

    conn.set_progress_handler(abort_if_expired, SQLITE_PROGRESS_INTERVAL)


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


@register_optimization_task
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


@register_optimization_task
def run_fstrim(dry_run=False):
    if not shutil.which("fstrim"):
        return None
    if dry_run:
        return "SSD partitions would be trimmed (fstrim)"
    if run_command(["fstrim", "-av"], use_sudo=True, capture=True).ok:
        return "SSD partitions trimmed (fstrim)"
    return None


@register_optimization_task
def run_fccache(dry_run=False):
    if not shutil.which("fc-cache"):
        return None
    if dry_run:
        return "System font cache would be refreshed"
    if run_command(["fc-cache"], capture=True).ok:
        return "System font cache refreshed"
    return None


@register_optimization_task
def run_sysctl_optimize(dry_run=False):
    """Optimize kernel memory/swap parameters if sysctl is available."""
    if not shutil.which("sysctl"):
        return None
    if dry_run:
        return "Kernel memory & cache parameters would be tuned (sysctl)"
    if not has_sudo():
        return None
    # Tune swappiness and vfs_cache_pressure to optimal desktop defaults
    cmd = ["sysctl", "vm.swappiness=10", "vm.vfs_cache_pressure=50"]
    if run_command(cmd, use_sudo=True, capture=True).ok:
        return "Kernel memory & cache parameters tuned (sysctl)"
    return None


@register_optimization_task
def run_tmpfiles_cleanup(dry_run=False):
    """Trigger systemd-tmpfiles clean rules."""
    if not shutil.which("systemd-tmpfiles"):
        return None
    if dry_run:
        return "Systemd tmpfiles clean rules would be processed"
    if run_command(["systemd-tmpfiles", "--clean"], use_sudo=True, capture=True).ok:
        return "Systemd tmpfiles clean rules processed"
    return None


@register_optimization_task
def run_ldconfig(dry_run=False):
    """Refresh dynamic linker bindings cache."""
    if not shutil.which("ldconfig"):
        return None
    if dry_run:
        return "Dynamic linker cache would be updated (ldconfig)"
    if run_command(["ldconfig"], use_sudo=True, capture=True).ok:
        return "Dynamic linker cache updated (ldconfig)"
    return None


@register_optimization_task
def run_locale_gen(dry_run=False):
    """Regenerate locale archive files if locale-gen is available."""
    if not shutil.which("locale-gen"):
        return None
    if dry_run:
        return "System locale archive would be regenerated"
    if run_command(["locale-gen"], use_sudo=True, capture=True).ok:
        return "System locale archive regenerated"
    return None


@register_optimization_task
def run_man_db_refresh(dry_run=False):
    """Update manual page database index."""
    if not shutil.which("mandb"):
        return None
    if dry_run:
        return "Manual page database index would be updated (mandb)"
    if run_command(["mandb", "-q"], capture=True).ok:
        return "Manual page database index updated (mandb)"
    return None


@register_optimization_task
def run_autostart_cleanup(dry_run=False):
    """Remove zombie autostart entries whose executable no longer exists."""
    autostart_dir = Path.home() / ".config" / "autostart"
    if not autostart_dir.exists():
        return None
    zombies = 0
    for desktop_file in autostart_dir.glob("*.desktop"):
        try:
            cmd = get_desktop_exec_command(desktop_file)
            is_zombie = bool(
                cmd
                and (
                    cmd.startswith("/")
                    and not os.path.exists(cmd)
                    or not cmd.startswith("/")
                    and not shutil.which(cmd)
                )
            )
            if is_zombie:
                if not dry_run:
                    desktop_file.unlink()
                zombies += 1
        except OSError:
            continue
    if zombies > 0:
        if dry_run:
            return f"Found {zombies} zombie autostart entries"
        return f"Removed {zombies} zombie autostart entries"
    return None


@register_optimization_task
def run_systemd_user_service_cleanup(dry_run=False):
    """Remove user service units whose ExecStart target no longer exists."""
    user_systemd_dir = Path.home() / ".config" / "systemd" / "user"
    if not user_systemd_dir.exists():
        return None

    broken_units = []
    for service_file in user_systemd_dir.glob("*.service"):
        try:
            exec_targets = _extract_service_exec_targets(service_file)
        except OSError:
            continue
        # Only consider broken if ALL exec targets are missing.
        if exec_targets and all(not _service_exec_target_exists(t) for t in exec_targets):
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


@register_optimization_task
def run_user_systemd_reset_failed(dry_run=False):
    """Reset failed user-level systemd unit states without touching D-Bus runtime files."""
    if not shutil.which("systemctl"):
        return None

    list_result = run_command(
        [
            "systemctl",
            "--user",
            "list-units",
            "--state=failed",
            "--no-legend",
            "--no-pager",
            "--plain",
        ],
        capture=True,
        timeout=10,
    )
    if not list_result.ok:
        return None

    failed_units = [line for line in list_result.stdout.splitlines() if line.strip()]
    if not failed_units:
        return None

    if dry_run:
        return f"Found {len(failed_units)} failed user systemd unit(s)"

    reset_result = run_command(
        ["systemctl", "--user", "reset-failed"],
        capture=True,
        timeout=10,
    )
    if reset_result.ok:
        return f"Reset {len(failed_units)} failed user systemd unit state(s)"
    return None


@register_optimization_task
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

        # Only reset if we have _MIN_RAM_SWAP_RATIOx the used swap available in RAM for safety
        if available > used_swap * _MIN_RAM_SWAP_RATIO:
            if dry_run:
                return f"Swap would be reset (Currently using {bytes_to_human(used_swap)})"

            # swapoff -a can take time as data is moved back to RAM
            if run_command(["swapoff", "-a"], use_sudo=True, timeout=120).ok:
                run_command(["swapon", "-a"], use_sudo=True, timeout=30)
                return f"Swap reset successful (Reclaimed {bytes_to_human(used_swap)})"
    except (OSError, ValueError):
        pass
    return None


@register_optimization_task
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


@register_optimization_task
def run_coredump_cleanup(dry_run=False):
    """Clean system coredump files from /var/lib/systemd/coredump."""
    coredump_dir = COREDUMP_DIR
    if not coredump_dir.exists():
        return None

    # Detect if there are actually core files to clean
    has_files = False
    try:
        # We use a limited glob to avoid overhead on huge directories
        has_files = any(coredump_dir.glob("core.*"))
    except OSError:
        # Likely permission error during glob, proceed to try sudo-based cleanup
        has_files = True

    if not has_files:
        return None

    if dry_run:
        return "System coredumps would be cleared"

    res = run_command(
        [
            "find",
            str(coredump_dir),
            "-maxdepth",
            "1",
            "-type",
            "f",
            "-name",
            "core.*",
            "-delete",
        ],
        use_sudo=True,
        capture=True,
    )
    if not res.ok:
        return None

    return "System coredumps cleared"


@register_optimization_task
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
            removed_ok, _ = safe_remove(link, use_trash=False)
            if removed_ok:
                removed += 1
        except OSError:
            continue

    if removed > 0:
        return f"Removed {removed} broken user symlink(s)"
    return None


def _extract_service_exec_targets(service_file: Path) -> list[str]:
    targets: list[str] = []
    for line in service_file.read_text(errors="ignore").splitlines():
        line = line.strip()
        if not line.startswith("ExecStart="):
            continue
        value = line.split("=", 1)[1].strip()
        if not value:
            continue
        try:
            parts = shlex.split(value)
        except ValueError:
            parts = value.split()
        if not parts:
            continue
        command = parts[0].lstrip("-@+!")
        if command:
            targets.append(command)
    return targets


def _service_exec_target_exists(command: str) -> bool:
    if command.startswith("/"):
        return Path(command).exists()
    return shutil.which(command) is not None


def _refresh_database(cmd: str, target_dir: Path, label: str, dry_run: bool) -> str | None:
    if not target_dir.exists() or not shutil.which(cmd):
        return None
    if dry_run:
        return f"{label} would be refreshed"
    if run_command([cmd, str(target_dir)], capture=True, timeout=30).ok:
        return f"{label} refreshed"
    return None


@register_optimization_task
def run_desktop_database_refresh(dry_run=False):
    return _refresh_database(
        "update-desktop-database",
        Path.home() / ".local/share/applications",
        "Desktop application database",
        dry_run,
    )


@register_optimization_task
def run_mime_database_refresh(dry_run=False):
    return _refresh_database(
        "update-mime-database",
        Path.home() / ".local/share/mime",
        "MIME database",
        dry_run,
    )


@register_optimization_task
def run_flatpak_repair(dry_run=False):
    """Verify and repair Flatpak system and user installations."""
    if not shutil.which("flatpak"):
        return None
    if dry_run:
        return "Flatpak system & user objects would be verified (flatpak repair)"
    # Repair user installation first, then system if sudo available
    run_command(["flatpak", "repair", "--user"], capture=True)
    if has_sudo():
        run_command(["flatpak", "repair"], use_sudo=True, capture=True)
    return "Flatpak storage objects verified (flatpak repair)"


@register_optimization_task
def run_tracker_miner_reset(dry_run=False):
    """Reset GNOME Tracker miner database if indices are corrupt/fragmented."""
    cmd = None
    if shutil.which("tracker3"):
        cmd = ["tracker3", "reset", "-s"]
    elif shutil.which("tracker"):
        cmd = ["tracker", "reset", "-r"]
    if not cmd:
        return None
    if dry_run:
        return "GNOME Tracker search index would be reset"
    if run_command(cmd, capture=True).ok:
        return "GNOME Tracker search index reset"
    return None


@register_optimization_task
def run_package_repo_refresh(dry_run=False):
    """Refresh PackageKit or APT-File software repository metadata."""
    cmd = None
    if shutil.which("pkcon"):
        cmd = ["pkcon", "refresh"]
    elif shutil.which("apt-file"):
        cmd = ["apt-file", "update"]
    if not cmd:
        return None
    if dry_run:
        return "Software repository index would be refreshed"
    if run_command(cmd, use_sudo=True, capture=True, timeout=30).ok:
        return "Software repository index refreshed"
    return None


def _authenticate_sudo_session(dry_run: bool) -> bool:
    if dry_run:
        return True

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
        f"{PURPLE}➔{RESET} System optimization requires admin access\n{PURPLE}➔{RESET} Password: "
    ):
        if system.SUDO_CANCELLED:
            print(f" {YELLOW}⚠️  Optimization cancelled by user.{RESET}", end="")
        else:
            print(f" {RED}✗{RESET} Authorization failed. Optimization skipped.\n")
        return False
    print(f" {GREEN}✓{RESET} Authorization successful.\n")
    return True


def optimize_system(dry_run: bool = False) -> bool | None:
    sys.stdout.write(CLEAR_SCREEN)
    sys.stdout.flush()
    print(f"\n{PURPLE}System Optimization{RESET}\n")
    print(f"{GRAY}Running maintenance tasks in parallel...{RESET}")

    if not _authenticate_sudo_session(dry_run):
        return False

    start_time = time.time()
    registered_tasks = OptimizationRegistry.tasks

    with ThreadPoolExecutor(max_workers=max(len(registered_tasks), 1)) as executor:
        futures = {executor.submit(task, dry_run=dry_run): task for task in registered_tasks}

        for future in as_completed(futures):
            try:
                result = future.result()
                if result:
                    opt_log(result, skipped=dry_run)
            except Exception:
                # Optimization runs independent maintenance tasks concurrently; one
                # task failure should not abort the rest of the batch.
                pass

    duration = time.time() - start_time
    print(f"\n{GREEN}{BOLD}✨ All tasks completed in {duration:.1f}s.{RESET}")
    return None
