import contextlib
import os
import re
import shlex
import shutil
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .core import system
from .core.browser_paths import BROWSER_PROFILE_TARGETS
from .core.config import get_use_trash
from .core.constants import (
    BOLD,
    CLEAR_LINE,
    CLEAR_SCREEN,
    FAIL,
    GRAY,
    OK,
    PURPLE,
    RESET,
    SKIP,
    SQLITE_PROGRESS_INTERVAL,
)
from .core.desktop_entry import get_desktop_exec_command
from .core.file_ops import (
    TRASH_UNAVAILABLE_REASON,
    bytes_to_human,
    comm_pattern,
    get_size,
    parse_size_from_text,
    safe_remove,
)
from .core.lock import is_file_locked, is_sqlite_busy
from .core.spinner import threaded_spinner
from .core.system import C_LOCALE_ENV, has_sudo, run_command
from .core.text import plural

# Lock to ensure parallel tasks don't corrupt the terminal output
print_lock = threading.Lock()
SQLITE_MAX_OPTIMIZE_SIZE = 100 * 1024 * 1024
SQLITE_MIN_FREE_BYTES = 5 * 1024 * 1024
SQLITE_MIN_FREE_RATIO = 0.10
SQLITE_VACUUM_TIMEOUT = 20
COREDUMP_DIR = Path("/var/lib/systemd/coredump")
# systemd-resolved's RuntimeDirectory, and the closest thing to a fork-free
# "is resolved in charge here". Fedora ships resolvectl in systemd itself, so
# the binary is present on a box whose DNS goes through NetworkManager's dnsmasq
# or a plain /etc/resolv.conf, where a flush has nothing to flush; Debian and
# Ubuntu put it in the systemd-resolved package, which can still be installed
# and masked. The unit sets RuntimeDirectoryPreserve=yes, so a resolved that ran
# once and was stopped leaves this behind -- the cost there is a failed command
# reported as nothing, not a wrong answer.
_RESOLVED_RUNTIME_DIR = Path("/run/systemd/resolve")
# glibc's own _PATH_NSCDSOCKET. nscd creates it on startup, so it answers "is
# there an nscd to talk to" -- the binary alone does not, and Debian ships the
# package for a daemon most machines never enable. /var/run is a symlink to /run
# on any systemd machine, so the literal glibc path resolves either way.
_NSCD_SOCKET = Path("/var/run/nscd/socket")
_MIN_RAM_SWAP_RATIO = 2
_SWAPS_TABLE = Path("/proc/swaps")
_ZRAM_DEVICE_RE = re.compile(r"^zram\d+$")
REPO_REFRESH_TIMEOUT = 120
UPDATEDB_TIMEOUT = 600
OPTIMIZATION_MAX_WORKERS = 4

# Debian keeps /sbin and /usr/sbin out of a non-root PATH on purpose, and Debian
# 13 still ships them unmerged -- its release notes call merging them by hand
# unsupported. Fedora folded them into /usr/bin, and Ubuntu lists them for every
# user, so shutil.which() alone answers "is fstrim installed?" correctly on those
# two and with a flat False on Debian for every tool that lives there.
_SBIN_DIRS = ("/usr/local/sbin", "/usr/sbin", "/sbin")


def _which_admin_tool(name: str) -> str | None:
    """Locate an administrative tool that a user's PATH may not cover.

    Only the lookup needs this: every caller runs the command through
    run_command(..., use_sudo=True), and sudo's secure_path already includes the
    sbin directories. Without the fallback the task does not fail, it reports the
    tool as absent and skips -- which is why this went unnoticed.
    """
    found = shutil.which(name)
    if found:
        return found
    for directory in _SBIN_DIRS:
        candidate = Path(directory) / name
        # is_file() follows symlinks, so this still resolves on a merged layout
        # where /usr/sbin points into /usr/bin.
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def opt_log(message, success=True, skipped=False):
    if skipped:
        icon = SKIP
        msg = f"{GRAY}{message} · skipped{RESET}"
    else:
        icon = OK if success else FAIL
        msg = f"{message}"

    with print_lock:
        # Use CLEAR_LINE to cleanly overwrite the running spinner line without collision
        print(f"{CLEAR_LINE}  {icon} {msg}")


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
    # "chromium-browser" is 16 characters, one over what the kernel's comm field
    # holds, and pgrep rejects a pattern that long instead of matching the
    # truncated name -- so an untruncated check reported a running Chromium as
    # idle and let its databases be vacuumed underneath it.
    return any(
        run_command(["pgrep", "-x", comm_pattern(name)], capture=True, timeout=1).ok
        for name in process_names
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
    if is_file_locked(db_path) or is_sqlite_busy(db_path):
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


# The biggest ones first: favicons.sqlite and places.sqlite routinely outgrow
# everything else in a profile, and webappsstore.sqlite grows with localStorage.
_FIREFOX_DB_NAMES = (
    "places.sqlite",
    "favicons.sqlite",
    "cookies.sqlite",
    "webappsstore.sqlite",
    "formhistory.sqlite",
    "storage.sqlite",
)

# Chromium's databases carry no extension, which is why _is_sqlite_database()
# checks the file header rather than trusting the name.
_CHROMIUM_DB_NAMES = (
    "History",
    "Favicons",
    "Web Data",
    "Top Sites",
    "Shortcuts",
    "Network/Cookies",
)

# The label is what a "skipped running app" message names, so the same browser
# installed twice stays one label. Everything else -- where the profiles sit for
# each install format, and which database family they hold -- comes from
# core.browser_paths, the same table protection and cache cleanup read. A snap's
# relocated profile root is then declared once instead of in three modules that
# drift apart, and tests/test_browser_paths.py keeps it that way.
_DB_NAMES_BY_ENGINE = {"gecko": _FIREFOX_DB_NAMES, "chromium": _CHROMIUM_DB_NAMES}

_BROWSER_DB_TARGETS: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = tuple(
    (
        target.name,
        target.procs,
        tuple(
            f"{glob}/{name}"
            for glob in target.profile_globs
            for name in _DB_NAMES_BY_ENGINE[target.engine]
        ),
    )
    for target in BROWSER_PROFILE_TARGETS
)


@register_optimization_task
def run_vacuum_all(dry_run=False):
    """Task to optimize all browser databases."""
    db_files: list[Path] = []
    busy_apps = set()
    home = Path.home()
    for app_name, process_names, patterns in _BROWSER_DB_TARGETS:
        if _is_any_process_running(list(process_names)):
            busy_apps.add(app_name)
            continue
        for pattern in patterns:
            # home.glob(pattern), not the old parent.exists() + parent.glob(name)
            # pair: that could only expand a wildcard in the final component, so
            # every ".mozilla/firefox/*/…" pattern asked whether a directory
            # literally named "*" existed, got False, and silently matched
            # nothing -- Firefox was never vacuumed at all.
            with contextlib.suppress(OSError):
                db_files.extend(f for f in home.glob(pattern) if f.is_file())
    # Profile globs from different roots cannot collide, but a duplicate would
    # VACUUM the same file twice and double-count it in the total.
    db_files = sorted(set(db_files))

    if busy_apps and not db_files:
        return f"{', '.join(sorted(busy_apps))} running; database optimization skipped"
    if not db_files:
        return None
    # The same tail on both messages: what was skipped is decided above, and a
    # preview and a real run skip the same apps for the same reason.
    suffix = (
        f"; skipped {plural(len(busy_apps), 'running app')}: {', '.join(sorted(busy_apps))}"
        if busy_apps
        else ""
    )
    if dry_run:
        return f"Found {plural(len(db_files), 'database')} to optimize{suffix}"

    total_saved = 0
    # Nested pool or just direct execution since we are already in a pool
    for db in db_files:
        total_saved += vacuum_single_db(db)

    saved_str = f" (compressed {bytes_to_human(total_saved)})" if total_saved > 0 else ""
    return f"Optimized {plural(len(db_files), 'browser database')}{saved_str}{suffix}"


@register_optimization_task
def run_fstrim(dry_run=False):
    if not _which_admin_tool("fstrim"):
        return None
    if dry_run:
        return "SSD partitions would be trimmed (fstrim)"
    if run_command(["fstrim", "-av"], use_sudo=True, capture=True).ok:
        return "SSD partitions trimmed (fstrim)"
    return None


@register_optimization_task
def run_fccache(dry_run=False):
    """Rebuild the fontconfig caches, user first and system too when possible.

    Plain ``fc-cache`` only writes ~/.cache/fontconfig -- the task used to run
    exactly that and report it as the system cache. /var/cache/fontconfig needs
    root, so it is a second pass rather than a replacement: the user cache is
    the one that goes stale after dropping a font into ~/.local/share/fonts, and
    it must still be refreshed on a machine with no sudo.
    """
    if not shutil.which("fc-cache"):
        return None
    if dry_run:
        return "Font caches would be refreshed (fc-cache)"
    if not run_command(["fc-cache"], capture=True).ok:
        return None
    if has_sudo() and run_command(["fc-cache"], use_sudo=True, capture=True).ok:
        return "User & system font caches refreshed (fc-cache)"
    return "User font cache refreshed (fc-cache)"


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
    if not _which_admin_tool("ldconfig"):
        return None
    if dry_run:
        return "Dynamic linker cache would be updated (ldconfig)"
    if run_command(["ldconfig"], use_sudo=True, capture=True).ok:
        return "Dynamic linker cache updated (ldconfig)"
    return None


@register_optimization_task
def run_locale_gen(dry_run=False):
    """Regenerate locale archive files if locale-gen is available."""
    if not _which_admin_tool("locale-gen"):
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


def _dns_cache_flushers() -> list[tuple[str, list[str], bool]]:
    """Every resolver cache on this machine: (label, command, may run as the user).

    A list rather than a first match, because the two stack: nscd caches what
    glibc looked up, and on a systemd machine glibc looked it up through
    resolved's stub, so flushing only the front one leaves the stale answer in
    the other. Each entry is proven live by the runtime file its daemon creates
    rather than by the presence of the tool, which proves nothing on either
    distro family (see _RESOLVED_RUNTIME_DIR and _NSCD_SOCKET).

    nscd is looked up through _which_admin_tool because it lives in /usr/sbin,
    which Debian keeps out of a non-root PATH.
    """
    flushers: list[tuple[str, list[str], bool]] = []
    if shutil.which("resolvectl") and _RESOLVED_RUNTIME_DIR.is_dir():
        # FlushCaches has been SD_BUS_VTABLE_UNPRIVILEGED with no polkit check
        # behind it since systemd v243 -- Debian 11, Ubuntu 20.04, and every
        # release after. Only v241 and older make it root's alone, which is what
        # the sudo retry is left for.
        flushers.append(("resolvectl", ["resolvectl", "flush-caches"], True))
    if _which_admin_tool("nscd") and _NSCD_SOCKET.exists():
        # No unprivileged attempt: nscd checks getuid() itself and answers
        # "Only root is allowed to use this option!" without touching the cache.
        flushers.append(("nscd", ["nscd", "-i", "hosts"], False))
    return flushers


def _dns_flush_report(labels: list[str], verb: str) -> str:
    """Build the task's one-line result, pluralised for how many caches it names."""
    noun = "cache" if len(labels) == 1 else "caches"
    return f"DNS resolver {noun} {verb} ({', '.join(labels)})"


@register_optimization_task
def run_dns_flush(dry_run=False):
    """Drop the resolver's cached answers so a stale record stops being served.

    This frees no disk space -- the caches are a few MB inside a running daemon
    -- and belongs here for the other reason Optimization exists: an entry
    cached before a DNS record moved keeps pointing at the old address for the
    rest of its TTL, and this is the switch that makes the machine ask again.
    """
    flushers = _dns_cache_flushers()
    if not flushers:
        return None
    if dry_run:
        return _dns_flush_report([label for label, _, _ in flushers], "would be flushed")

    flushed = []
    for label, cmd, may_run_as_user in flushers:
        if may_run_as_user and run_command(cmd, capture=True).ok:
            flushed.append(label)
            continue
        # The batch already holds the sudo session, so the retry costs a fork
        # and no prompt.
        if run_command(cmd, use_sudo=True, capture=True).ok:
            flushed.append(label)
    if not flushed:
        return None
    return _dns_flush_report(flushed, "flushed")


@register_optimization_task
def run_autostart_cleanup(dry_run=False):
    """Remove zombie autostart entries whose executable no longer exists."""
    autostart_dir = Path.home() / ".config" / "autostart"
    if not autostart_dir.exists():
        return None
    zombies = 0
    kept_no_trash = 0
    use_trash = get_use_trash()
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
                    ok, reason = safe_remove(desktop_file, use_trash=use_trash)
                    if not ok:
                        # No silent downgrade to a permanent delete: this task
                        # runs unattended in a worker thread, so there is nobody
                        # to consent to an unrecoverable removal. Setting
                        # use_trash=false in config.json is that consent, given
                        # ahead of time, and then this branch never runs.
                        if reason == TRASH_UNAVAILABLE_REASON:
                            kept_no_trash += 1
                        continue
                zombies += 1
        except OSError:
            continue
    if zombies > 0:
        if dry_run:
            return f"Found {zombies} zombie autostart entries"
        message = f"Removed {zombies} zombie autostart entries"
        if kept_no_trash:
            message += f" ({kept_no_trash} kept: no trash backend available)"
        return message
    if kept_no_trash:
        return f"Kept {kept_no_trash} zombie autostart entries (no trash backend available)"
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
        return f"Removed {plural(removed, 'broken user systemd service')}"

    return f"Found {plural(len(broken_units), 'broken user systemd service')}"


def _reset_failed_units(
    scope_args: list[str], label: str, dry_run: bool, *, use_sudo: bool
) -> str | None:
    """Clear failed unit state for one systemd scope, counting what was cleared.

    The units are listed first so the message can name a number: bare
    ``reset-failed`` succeeds on a clean system too, which would report work that
    did not happen. Listing needs no privileges at either scope; only the reset
    itself does, and only for the system manager.
    """
    if not shutil.which("systemctl"):
        return None

    list_result = run_command(
        [
            "systemctl",
            *scope_args,
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
        return f"Found {plural(len(failed_units), f'failed {label} systemd unit')}"

    reset_result = run_command(
        ["systemctl", *scope_args, "reset-failed"],
        capture=True,
        timeout=10,
        use_sudo=use_sudo,
    )
    if reset_result.ok:
        return f"Reset {plural(len(failed_units), f'failed {label} systemd unit state')}"
    return None


@register_optimization_task
def run_user_systemd_reset_failed(dry_run=False):
    """Reset failed user-level systemd unit states without touching D-Bus runtime files."""
    return _reset_failed_units(["--user"], "user", dry_run, use_sudo=False)


@register_optimization_task
def run_system_systemd_reset_failed(dry_run=False):
    """Reset failed system-level unit states, which only root can clear.

    Failed state is bookkeeping, not the failure itself: clearing it makes the
    next ``systemctl --failed`` show what has broken *since*, rather than a list
    that also carries units nobody has looked at in months. Gated on sudo so a
    machine without it skips instead of logging a permission error every run.
    """
    if not has_sudo():
        return None
    return _reset_failed_units([], "system", dry_run, use_sudo=True)


def _swap_is_zram_backed() -> bool:
    """True when any active swap device is a zram block device.

    ``swapoff -a`` disables every swap device, but ``swapon -a`` only re-enables
    what /etc/fstab lists. zram swap is created and enabled by
    systemd-zram-setup@zramN.service and has no fstab entry, so the pair below
    would leave the machine with no swap at all until the next boot -- the
    opposite of the reclaim it advertises. Fedora enables zram swap out of the
    box, and so does anything carrying zram-generator or Debian's zram-tools.
    Ubuntu is not in that group: since 17.04 its installer creates a /swap.img
    swapfile and lists it in fstab, which swapon -a does restore.

    One zram device disables the task even alongside a real swap partition,
    because ``swapoff -a`` is all-or-nothing: the partition would come back and
    the zram device would not.

    Reading /proc/swaps rather than shelling out to swapon(8): it is the table
    swapon itself reports, and this runs in a pool worker where a subprocess per
    task adds up.
    """
    try:
        table = _SWAPS_TABLE.read_text().splitlines()
    except OSError:
        # Nothing proves the reset is reversible, so call it unsafe -- the same
        # bias as _points_at_transient_mount: on unreadable input, keep what is.
        return True
    # First line is the column header; the first field is the device or file.
    for line in table[1:]:
        fields = line.split()
        if fields and _ZRAM_DEVICE_RE.match(os.path.basename(fields[0])):
            return True
    return False


@register_optimization_task
def run_swap_management(dry_run=False):
    """Reset swap if RAM is plentiful to reduce micro-stutter."""
    if not _which_admin_tool("swapoff") or not _which_admin_tool("swapon"):
        return None
    if _swap_is_zram_backed():
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

    res = run_command(
        ["journalctl", "--vacuum-time=3d"], use_sudo=True, capture=True, env=C_LOCALE_ENV
    )
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


def _broken_symlink_search_dirs() -> list[Path]:
    """Directories swept for dangling symlinks.

    Only ~/.local/bin is swept by default: it holds launcher links whose targets
    are package-managed, so a dangling entry there is genuinely dead. ~/Desktop
    and ~/Documents hold links the user placed by hand — often to removable or
    network storage that is simply not mounted right now — so they are opt-in
    via TOPO_SYMLINK_SCAN_USER_DIRS=1.
    """
    dirs = [Path.home() / ".local/bin"]
    if os.environ.get("TOPO_SYMLINK_SCAN_USER_DIRS") == "1":
        dirs.extend([Path.home() / "Desktop", Path.home() / "Documents"])
    return dirs


# Prefixes whose absence means "not mounted", not "target deleted". Removing a
# link into them would destroy the only pointer to data that comes back on the
# next mount.
_TRANSIENT_MOUNT_PREFIXES = ("/media/", "/mnt/", "/run/media/", "/run/user/")
_GVFS_PATH_MARKERS = ("/gvfs/", "/.gvfs/")


def _points_at_transient_mount(link: Path) -> bool:
    """True when a dangling link's target only looks missing because of a mount."""
    try:
        target = os.fspath(link.readlink())
    except OSError:
        # Unreadable link: treat as transient and keep it rather than guess.
        return True

    # A relative target resolves against the link's own directory, so it must be
    # joined before any prefix test — otherwise "../../../media/usb/x" reads as
    # a harmless relative name and slips past the guard.
    absolute = os.path.normpath(os.path.join(os.fspath(link.parent), target))
    if absolute.startswith(_TRANSIENT_MOUNT_PREFIXES):
        return True
    # The trailing slash makes the marker match the mount root itself (~/.gvfs)
    # as well as anything under it.
    return any(marker in f"{absolute}/" for marker in _GVFS_PATH_MARKERS)


@register_optimization_task
def run_broken_symlink_cleanup(dry_run=False):
    """Remove broken symlinks in common user directories."""
    broken = []
    for d in _broken_symlink_search_dirs():
        if not d.exists():
            continue
        try:
            for item in d.iterdir():
                if item.is_symlink() and not item.exists():
                    if _points_at_transient_mount(item):
                        continue
                    broken.append(item)
        except OSError:
            continue

    if not broken:
        return None

    if dry_run:
        return f"Found {len(broken)} broken user symlinks"

    removed = 0
    kept_no_trash = 0
    # Recoverable by default: a dangling link is still the only record of what it
    # once pointed at, and this task runs unattended in a worker thread where
    # nobody can consent to an unrecoverable delete. Only config.json's
    # use_trash=false, set in advance, opts out.
    use_trash = get_use_trash()
    for link in broken:
        try:
            removed_ok, reason = safe_remove(link, use_trash=use_trash)
            if removed_ok:
                removed += 1
            elif reason == TRASH_UNAVAILABLE_REASON:
                kept_no_trash += 1
        except OSError:
            continue

    if removed > 0:
        message = f"Removed {plural(removed, 'broken user symlink')}"
        if kept_no_trash:
            message += f" ({kept_no_trash} kept: no trash backend available)"
        return message
    if kept_no_trash:
        return f"Kept {plural(kept_no_trash, 'broken user symlink')} (no trash backend available)"
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
def run_glib_schema_compile(dry_run=False):
    """Recompile locally installed GSettings schemas into gschemas.compiled."""
    return _refresh_database(
        "glib-compile-schemas",
        Path.home() / ".local/share/glib-2.0/schemas",
        "User GSettings schema cache",
        dry_run,
    )


@register_optimization_task
def run_icon_cache_refresh(dry_run=False):
    """Rebuild the icon cache of every theme under ~/.local/share/icons.

    gtk-update-icon-cache takes one theme directory, not the icons root, and
    exits non-zero on a directory without index.theme -- so the themes are
    enumerated by that file here rather than handed the root or discovered with
    iterdir. Only user-installed themes are touched; the ones under /usr are
    rebuilt by package triggers.

    Passing just -q and -f: GTK3 reads -t as --ignore-theme-index while GTK4
    reads it as --index-only, so the flag means two different things depending on
    which build is on PATH.
    """
    if not shutil.which("gtk-update-icon-cache"):
        return None
    try:
        themes = sorted(
            p.parent for p in (Path.home() / ".local/share/icons").glob("*/index.theme")
        )
    except OSError:
        return None
    if not themes:
        return None
    if dry_run:
        return f"{plural(len(themes), 'user icon theme cache')} would be rebuilt"
    rebuilt = 0
    for theme in themes:
        if run_command(
            ["gtk-update-icon-cache", "-q", "-f", str(theme)], capture=True, timeout=30
        ).ok:
            rebuilt += 1
    if rebuilt == 0:
        return None
    return f"Rebuilt {plural(rebuilt, 'user icon theme cache')}"


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


def _systemd_timer_enabled(unit_names: tuple[str, ...]) -> bool:
    """True when systemd already owns one of these periodic jobs.

    ``systemctl is-enabled`` prints one verdict per unit and exits non-zero when
    any of them is not enabled, so the answer is in the output lines rather than
    the status code -- a units list that includes a name this distro does not
    ship is normal here, not an error.
    """
    if not shutil.which("systemctl"):
        return False
    result = run_command(["systemctl", "is-enabled", *unit_names], capture=True, timeout=10)
    return any(line.strip() == "enabled" for line in result.stdout.splitlines())


# plocate and mlocate name their timer differently, and some distros ship a
# plain updatedb.timer. Any one of them being enabled means the index is already
# being rebuilt on a schedule.
_UPDATEDB_TIMERS = ("plocate-updatedb.timer", "mlocate-updatedb.timer", "updatedb.timer")

# The same job is just as often a cron entry: Debian 13's plocate ships
# /etc/cron.daily/plocate next to its timer, and mlocate on older Debian and
# Ubuntu ships only the cron half. A systemd-only check would rebuild the index a
# second time on exactly those machines.
_UPDATEDB_CRON_JOBS = (
    Path("/etc/cron.daily/plocate"),
    Path("/etc/cron.daily/mlocate"),
    Path("/etc/cron.daily/updatedb"),
    Path("/etc/cron.daily/locate"),
)


def _updatedb_is_scheduled() -> bool:
    """True when a timer or a cron job already rebuilds the locate index."""
    if _systemd_timer_enabled(_UPDATEDB_TIMERS):
        return True
    # run-parts skips a cron.daily entry that is not executable, so presence
    # alone would read a disabled job as an active one.
    return any(job.is_file() and os.access(job, os.X_OK) for job in _UPDATEDB_CRON_JOBS)


@register_optimization_task
def run_locate_db_refresh(dry_run=False):
    """Rebuild the locate(1) index, unless the distro already owns that job.

    updatedb walks every mounted filesystem, which makes it the one task here
    that can outlast all the others combined. So it is skipped whenever a timer
    or a cron entry is in place -- plocate ships one enabled by default on Fedora
    and Debian, and running it again would only duplicate work that already
    happens daily. What is left is the case neither covers: an index that exists
    because someone installed plocate, with nothing scheduled to keep it current.
    """
    if not _which_admin_tool("updatedb") or not has_sudo():
        return None
    if _updatedb_is_scheduled():
        return None
    if dry_run:
        return "locate database would be rebuilt (updatedb)"
    if run_command(["updatedb"], use_sudo=True, capture=True, timeout=UPDATEDB_TIMEOUT).ok:
        return "locate database rebuilt (updatedb)"
    return None


# Native package manager first, PackageKit after: pkcon reaches the same backend
# but only where PackageKit is installed and configured, and it reports failures
# one layer removed from whatever actually broke.
_REPO_REFRESH_COMMANDS: tuple[tuple[str, list[str]], ...] = (
    ("dnf5", ["dnf5", "makecache"]),
    ("dnf", ["dnf", "makecache"]),
    ("zypper", ["zypper", "--non-interactive", "refresh"]),
    # -Fy syncs the *files* database only. Never -Sy: refreshing the package
    # database without upgrading is what leaves an Arch box half-upgraded.
    ("pacman", ["pacman", "-Fy", "--noconfirm"]),
    # apt-get rather than apt, which prints a warning that its CLI is not meant
    # for scripts. ``update`` only downloads metadata; nothing is installed or
    # upgraded, so it is the exact counterpart of dnf makecache.
    ("apt-get", ["apt-get", "update", "-q"]),
    ("pkcon", ["pkcon", "refresh"]),
    ("apt-file", ["apt-file", "update"]),
)


@register_optimization_task
def run_package_repo_refresh(dry_run=False):
    """Refresh the software repository metadata index."""
    cmd = next((command for tool, command in _REPO_REFRESH_COMMANDS if shutil.which(tool)), None)
    if not cmd:
        return None
    if dry_run:
        return "Software repository index would be refreshed"
    # This downloads metadata, so the old 30s ceiling was really a slow-mirror
    # detector. A cut-off refresh is retried next run, never left half-applied.
    if run_command(cmd, use_sudo=True, capture=True, timeout=REPO_REFRESH_TIMEOUT).ok:
        return "Software repository index refreshed"
    return None


def optimize_system(dry_run: bool = False) -> bool:
    """Run the maintenance task batch; False when it never ran (sudo declined).

    A task that raises is logged and the batch continues, and that stays a
    success: the tasks are independent maintenance chores, so one unavailable
    tool (no fstrim, no fc-cache) must not make `topo optimize` look broken.
    """
    sys.stdout.write(CLEAR_SCREEN)
    sys.stdout.flush()
    print(f"\n{PURPLE}System Optimization{RESET}\n")

    if not system.authenticate_sudo_session(
        dry_run, request_subject="Optimization tasks", action="optimization"
    ):
        return False

    start_time = time.time()
    registered_tasks = OptimizationRegistry.tasks

    def render_optimization_spinner(frame: str) -> None:
        # Share print_lock with opt_log: without it a task result printed by a
        # worker can interleave with a spinner frame mid-write and get clobbered
        # by the frame's leading CLEAR_LINE. Hold the lock only around the write,
        # never across the wait.
        with print_lock:
            sys.stdout.write(
                f"{CLEAR_LINE}  {PURPLE}{frame}{RESET} {GRAY}Running maintenance tasks in parallel...{RESET}"
            )
            sys.stdout.flush()

    worker_count = min(max(len(registered_tasks), 1), OPTIMIZATION_MAX_WORKERS)
    try:
        with (
            threaded_spinner(render_optimization_spinner),
            ThreadPoolExecutor(max_workers=worker_count) as executor,
        ):
            futures = {executor.submit(task, dry_run=dry_run): task for task in registered_tasks}

            for future in as_completed(futures):
                try:
                    result = future.result()
                    if result:
                        opt_log(result, skipped=dry_run)
                except Exception as exc:
                    # Optimization runs independent maintenance tasks concurrently; one
                    # task failure should not abort the rest of the batch.
                    task = futures[future]
                    opt_log(f"{task.__name__} failed ({type(exc).__name__})", success=False)
    finally:
        sys.stdout.write(f"{CLEAR_LINE}")
        sys.stdout.flush()

    duration = time.time() - start_time
    print(f"\n{OK} {BOLD}All tasks completed in {duration:.1f}s.{RESET}")
    return True
