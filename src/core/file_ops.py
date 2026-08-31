import contextlib
import functools
import json
import os
import re
import shutil
import stat
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import get_min_age_days
from .constants import SECONDS_PER_DAY, WARN
from .engine import get_core_binary, get_rust_scan_data
from .paths import get_state_dir
from .system import run_command
from .text import is_unsafe_display_char
from .whitelist import (
    CRITICAL_PREFIX_PATHS,
    DELETION_CRITICAL_EXACT_PATHS,
    get_hard_protection_reason,
    is_protected,
    is_system_cleanable_content,
)

# Global registry to track handled paths across modules
CLEANED_PATHS: set[str] = set()
CACHEDIR_TAG_FILE = "CACHEDIR.TAG"
CACHEDIR_TAG_SIGNATURE = "Signature: 8a477f597d28d172789f06886806bc55"
TRASH_UNAVAILABLE_REASON = "No trash utility available; refusing to permanently delete"

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


@functools.cache
def _which_cached(name: str) -> str | None:
    return shutil.which(name)


def get_deletion_log_path() -> Path:
    """Return the audit log path for destructive file operations."""
    if override := os.environ.get("TOPO_DELETE_LOG"):
        return Path(override).expanduser()
    return get_state_dir() / "deletions.log"


def _sanitize_audit_field(value: str) -> str:
    """Escape control characters that could forge log lines or trigger ANSI injection when cat/less-ed.

    The character *set* is shared with sanitize_for_display() so display and log
    can never disagree about what is unsafe; only the output differs — the log
    escapes (recoverable, greppable) where the UI replaces.
    """
    out = []
    for ch in value:
        code = ord(ch)
        if ch == "\\":
            out.append("\\\\")
        elif is_unsafe_display_char(code):
            out.append(f"\\x{code:02x}" if code <= 0xFF else f"\\u{code:04x}")
        else:
            out.append(ch)
    return "".join(out)


_AUDIT_WARNINGS_EMITTED: set[str] = set()


def _warn_audit_dropped(reason: str) -> None:
    """Say once, on stderr, that an audit record was dropped.

    Silence used to be the whole failure mode here: a symlinked or foreign-owned
    log made every deletion go unrecorded with nothing to notice it by. The
    reason is deduplicated so a long cleanup run cannot spam the screen.
    """
    if reason in _AUDIT_WARNINGS_EMITTED:
        return
    _AUDIT_WARNINGS_EMITTED.add(reason)
    print(f"{WARN} topo: deletion audit record dropped: {reason}", file=sys.stderr)


def _prepare_audit_log(log_path: Path) -> bool:
    """Return True when *log_path* is safe to append to, tightening it if needed.

    The audit trail records what Topo deleted, so it must not be redirectable by
    anyone else: a symlink, a non-regular file, or an entry owned by another user
    is refused instead of followed. Because mkdir/open modes only apply at
    creation time, a log left group- or world-readable by an older release is
    also reclaimed to 0600 here, as is Topo's own state directory to 0700.
    """
    parent = log_path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        _warn_audit_dropped(f"cannot create {parent} ({exc})")
        return False

    uid = os.getuid()
    try:
        dir_st = parent.lstat()
    except OSError as exc:
        _warn_audit_dropped(f"cannot inspect {parent} ({exc})")
        return False
    if stat.S_ISLNK(dir_st.st_mode) or not stat.S_ISDIR(dir_st.st_mode):
        _warn_audit_dropped(f"{parent} is not a real directory")
        return False
    # Running as root (sudo topo) legitimately sees a log owned by the invoking
    # user, so ownership is only enforced for unprivileged runs — where a foreign
    # owner really does mean somebody else planted the file.
    if uid != 0 and dir_st.st_uid != uid:
        _warn_audit_dropped(f"{parent} is owned by uid {dir_st.st_uid}, not by this user")
        return False
    if stat.S_IMODE(dir_st.st_mode) & 0o077 and parent == get_state_dir():
        # Only Topo's own state directory is tightened. TOPO_DELETE_LOG may point
        # into a directory shared with other users (/tmp, /var/log), and under
        # sudo a blind chmod 0700 there would lock everyone else out of it.
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)

    try:
        file_st = log_path.lstat()
    except FileNotFoundError:
        return True
    except OSError as exc:
        _warn_audit_dropped(f"cannot inspect {log_path} ({exc})")
        return False
    if stat.S_ISLNK(file_st.st_mode):
        _warn_audit_dropped(f"{log_path} is a symlink; refusing to follow it")
        return False
    if not stat.S_ISREG(file_st.st_mode):
        _warn_audit_dropped(f"{log_path} is not a regular file")
        return False
    if uid != 0 and file_st.st_uid != uid:
        _warn_audit_dropped(f"{log_path} is owned by uid {file_st.st_uid}, not by this user")
        return False
    if stat.S_IMODE(file_st.st_mode) & 0o177:
        with contextlib.suppress(OSError):
            os.chmod(log_path, 0o600)
    return True


def audit_log_is_trusted(log_path: Path) -> bool:
    """Read-side counterpart of _prepare_audit_log().

    History rendering shows the log back to the user, so a log that somebody else
    can redirect or rewrite must not be read at all — otherwise a planted symlink
    turns into fabricated deletion history.
    """
    try:
        st = log_path.lstat()
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
        _warn_audit_dropped(f"{log_path} is not a regular file; not reading history from it")
        return False
    if os.getuid() != 0 and st.st_uid != os.getuid():
        _warn_audit_dropped(f"{log_path} is owned by uid {st.st_uid}; not reading history from it")
        return False
    return True


def record_deletion_audit(
    path: str | Path,
    mode: str,
    status: str,
    size_bytes: int | None = None,
) -> None:
    """Append a best-effort deletion audit event."""
    log_path = get_deletion_log_path()
    try:
        size = "unknown" if size_bytes is None else str(max(int(size_bytes), 0))
    except (TypeError, ValueError):
        size = "unknown"
    timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
    if not _prepare_audit_log(log_path):
        return
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(log_path, flags, 0o600)
        with open(fd, "a", encoding="utf-8", closefd=True) as f:
            safe_path = _sanitize_audit_field(str(Path(path).expanduser()))
            f.write(f"{timestamp}\t{mode}\t{size}\t{status}\t{safe_path}\n")
    except OSError as exc:
        _warn_audit_dropped(f"cannot write {log_path} ({exc})")


def validate_path_for_deletion(
    path: str | Path,
    allow_app_data_removal: bool = False,
    allow_self_removal: bool = False,
) -> tuple[bool, str]:
    """Validate a raw deletion target before size checks or unlink attempts."""
    raw_text = os.fspath(path)
    if not raw_text:
        return False, "Path is empty"
    if _CONTROL_CHARS_RE.search(raw_text):
        return False, "Path contains control characters"
    if not Path(raw_text).expanduser().is_absolute():
        return False, "Path must be absolute"
    if any(part == ".." for part in Path(raw_text).parts):
        return False, "Path traversal is not allowed"

    raw_path = Path(raw_text).expanduser()
    try:
        resolved_path = raw_path.resolve(strict=False)
    except OSError:
        resolved_path = raw_path.absolute()

    if allow_app_data_removal:
        if (reason := get_hard_protection_reason(resolved_path)) and not (
            allow_self_removal and reason in ("Topo installation", "Topo configuration")
        ):
            return False, f"Path is hard-protected: {reason}"
    elif is_protected(resolved_path):
        return False, "Path is whitelisted"
    if resolved_path == Path("/") or resolved_path in DELETION_CRITICAL_EXACT_PATHS:
        return False, "Refusing to delete critical system path"
    for critical in CRITICAL_PREFIX_PATHS:
        if (
            resolved_path == critical or critical in resolved_path.parents
        ) and not is_system_cleanable_content(resolved_path):
            return False, "Refusing to delete critical system path"
    return True, ""


def register_cleaned_path(path: str | Path | None):
    """Registers a path as handled to avoid double-cleaning."""
    if path:
        p = Path(path).expanduser().resolve()
        CLEANED_PATHS.add(str(p))


# The kernel stores a task's name in comm, which holds TASK_COMM_LEN (16) bytes
# including the NUL, so anything the process was started as gets cut to 15
# characters.
_COMM_MAX_LEN = 15


def comm_pattern(process_name: str) -> str:
    """Return *process_name* cut to what ``pgrep -x`` and ``pkill -x`` can match.

    Both match against the kernel's comm field, and procps-ng refuses a pattern
    longer than that field can hold -- a warning on stderr and exit 1 -- rather
    than matching the truncated name. Untruncated, every name over 15 characters
    ("google-chrome-stable", "chromium-browser", "brave-browser-stable") reported
    "not running" for a process that was running, and no signal ever reached it.
    """
    return process_name[:_COMM_MAX_LEN]


def is_app_running(process_name: str) -> bool:
    """Check if an application is currently running."""
    return run_command(["pgrep", "-x", comm_pattern(process_name)], capture=True, timeout=5).ok


def running_process_comms() -> dict[str, list[int]]:
    """Map each running process's comm to the PIDs carrying it, in one /proc pass.

    Every `pgrep -x` costs a fork, and a caller with a list of candidate names --
    an app's id, its de-prefixed forms, each hyphen segment, every matching
    .desktop Exec -- pays that fork per name, per app. Reading the table once
    turns all of those questions into dict lookups: one directory listing plus a
    one-line read per process.

    The kernel already stores comm truncated to 15 characters, so the keys are
    directly comparable to comm_pattern() output and to a `pgrep -x` pattern.
    """
    running: dict[str, list[int]] = {}
    with contextlib.suppress(OSError):
        for proc in Path("/proc").iterdir():
            if not proc.name.isdigit():
                continue
            # A process can exit between the listing and the read, and a foreign
            # namespace entry can refuse it; either way it is simply not running
            # as far as the caller is concerned.
            with contextlib.suppress(OSError, ValueError):
                # A process name is 15 bytes of whatever the process asked for,
                # not text: prctl() takes any bytes it is given. The suppress
                # above does catch the strict decode's UnicodeDecodeError, so this
                # never crashed -- it dropped the PID instead, which meant a
                # process whose comm was not UTF-8 did not count as running at all
                # in the check `topo uninstall` makes before removing an app.
                comm = (proc / "comm").read_text(errors="replace").strip()
                if comm:
                    running.setdefault(comm, []).append(int(proc.name))
    return running


def bytes_to_human(n_bytes: int) -> str:
    """Converts bytes to human readable format using binary units."""
    val = float(n_bytes)
    for unit in ["B", "KiB", "MiB", "GiB", "TiB"]:
        if val < 1024:
            return f"{val:.1f} {unit}" if unit != "B" else f"{int(val)} {unit}"
        val /= 1024
    return f"{val:.1f} PiB"


def has_valid_cachedir_tag(path: str | Path) -> bool:
    """Return True when a directory contains a valid CACHEDIR.TAG marker."""
    path = Path(path).expanduser()
    tag_path = path / CACHEDIR_TAG_FILE
    try:
        if (
            path.is_symlink()
            or not path.is_dir()
            or tag_path.is_symlink()
            or not tag_path.is_file()
        ):
            return False
        with tag_path.open("r", encoding="utf-8", errors="ignore") as f:
            return f.read(len(CACHEDIR_TAG_SIGNATURE)) == CACHEDIR_TAG_SIGNATURE
    except OSError:
        return False


def get_size(path: str | Path) -> int:
    """Recursive size calculation in bytes (delegates directory walks to Rust engine when available)."""
    p = Path(path)
    if not p.exists():
        return 0
    if p.is_file() or p.is_symlink():
        try:
            return p.stat().st_size
        except OSError:
            return 0

    if p.is_dir():
        try:
            fast_size = _get_fast_scan_data(p)
            if fast_size is not None:
                return _coerce_non_negative_size(fast_size.get("total_size_bytes")) or 0
        except Exception:
            pass

    total = 0
    try:
        with os.scandir(p) as it:
            for entry in it:
                if entry.is_symlink() or entry.is_file():
                    total += entry.stat(follow_symlinks=False).st_size
                elif entry.is_dir():
                    total += get_size(entry.path)
    except OSError:
        pass
    return total


def _coerce_non_negative_size(value: Any) -> int | None:
    try:
        return max(int(value), 0)
    except (TypeError, ValueError):
        return None


def _get_fast_scan_data(path: Path) -> dict[str, Any] | None:
    data = get_rust_scan_data(path)
    return data if isinstance(data, dict) else None


def get_size_fast(path: str | Path) -> int:
    """Size of a directory using the Rust engine, falling back to get_size().

    The engine now counts hidden files (skip_hidden=false), so its total matches
    the pure-Python walk while being far faster on huge trees (node_modules, the
    cargo registry, model caches). Files and engine-less environments fall back to
    the exact Python implementation.
    """
    p = Path(path)
    if p.is_dir():
        data = _get_fast_scan_data(p)
        if data is not None:
            return _coerce_non_negative_size(data.get("total_size_bytes")) or 0
    return get_size(p)


def get_direct_child_sizes_fast(path: str | Path) -> dict[str, int] | None:
    """Return immediate child sizes from one Rust scan.

    None means no usable fast scan is available and callers should fall back to
    per-child sizing. An empty dict means the scan succeeded but found no
    non-zero direct children.
    """
    p = Path(path)
    if not p.is_dir():
        return None

    data = _get_fast_scan_data(p)
    if data is None:
        return None

    subdirs = data.get("subdirs")
    if not isinstance(subdirs, dict):
        return None

    child_sizes: dict[str, int] = {}
    for name, size in subdirs.items():
        size_bytes = _coerce_non_negative_size(size)
        if size_bytes is not None:
            child_sizes[str(name)] = size_bytes
    return child_sizes


def safe_remove(
    path: str | Path,
    use_trash: bool = True,
    dry_run: bool = False,
    allow_app_data_removal: bool = False,
    allow_self_removal: bool = False,
    known_size_bytes: int | None = None,
) -> tuple[bool, str]:
    """Safe removal with trash support and protection checks."""
    raw_path = Path(path).expanduser()
    mode = "trash" if use_trash else "permanent"

    valid, reason = validate_path_for_deletion(
        path,
        allow_app_data_removal=allow_app_data_removal,
        allow_self_removal=allow_self_removal,
    )
    if not valid:
        record_deletion_audit(raw_path, mode, "rejected-validation")
        return False, reason

    if not raw_path.exists() and not raw_path.is_symlink():
        record_deletion_audit(raw_path, mode, "missing", 0)
        return False, "Path does not exist"

    if known_size_bytes is None:
        size_bytes = get_size_fast(raw_path)
    else:
        try:
            size_bytes = max(int(known_size_bytes), 0)
        except (TypeError, ValueError):
            size_bytes = get_size_fast(raw_path)
    if dry_run:
        record_deletion_audit(raw_path, mode, "dry-run", size_bytes)
        return True, "Dry run"

    try:
        # Re-resolve to guard against TOCTOU symlink replacement for both trash & permanent branches
        try:
            re_resolved = raw_path.resolve(strict=False)
        except OSError:
            re_resolved = raw_path.absolute()
        re_valid, re_reason = validate_path_for_deletion(
            re_resolved,
            allow_app_data_removal=allow_app_data_removal,
            allow_self_removal=allow_self_removal,
        )
        if not re_valid:
            record_deletion_audit(raw_path, mode, "rejected-toctou")
            return False, f"TOCTOU check failed: {re_reason}"

        if use_trash:
            if (
                _which_cached("gio")
                and run_command(["gio", "trash", str(raw_path)], capture=True, timeout=30).ok
            ):
                record_deletion_audit(raw_path, "trash", "trashed-gio", size_bytes)
                return True, "Moved to trash (gio)"
            if (
                _which_cached("trash-put")
                and run_command(["trash-put", str(raw_path)], capture=True, timeout=30).ok
            ):
                record_deletion_audit(raw_path, "trash", "trashed-trash-cli", size_bytes)
                return True, "Moved to trash (trash-cli)"
            record_deletion_audit(raw_path, "trash", "trash-failed", size_bytes)
            return False, TRASH_UNAVAILABLE_REASON

        if raw_path.is_dir() and not raw_path.is_symlink():
            shutil.rmtree(raw_path)
        else:
            # No "is it in use?" gate here on purpose: unlink() is safe against
            # open file descriptors on Linux (the inode outlives the name), so
            # probing for locks only ever produced false refusals — a read-only
            # 0444 file could never be deleted at all. Lock probing belongs to
            # database maintenance, where a writer really must not be disturbed.
            raw_path.unlink()
        record_deletion_audit(raw_path, "permanent", "deleted", size_bytes)
        return True, "Permanently deleted"
    except OSError as e:
        failed_mode = "permanent" if use_trash else mode
        record_deletion_audit(raw_path, failed_mode, "failed", size_bytes)
        return False, str(e)


def _has_recent_content(path: Path, cutoff: float) -> bool:
    """Return True if any file under *path* has been touched after *cutoff*."""
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_atime >= cutoff or st.st_mtime >= cutoff:
                    return True
                if entry.is_dir(follow_symlinks=False) and _has_recent_content(
                    Path(entry.path), cutoff
                ):
                    return True
    except OSError:
        pass
    return False


def _get_path_stats(path: Path) -> dict[str, Any] | None:
    """Return size and newest activity from one Rust traversal."""
    binary = get_core_binary()
    if binary is None:
        return None
    result = run_command([str(binary), "--stats", str(path)], capture=True, timeout=300)
    if not result.ok:
        return None
    try:
        data = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def is_reclaimable_entry_type(mode: int) -> bool:
    """Return True when a stat mode describes something worth deleting for space.

    Sockets, FIFOs and device nodes hold no data, so removing one frees nothing
    while breaking whatever process is on the other end. The case that actually
    bites is /tmp: Debian and Ubuntu ship "use-ssh-agent" in
    /etc/X11/Xsession.options, so the session's agent lives at
    /tmp/ssh-XXXXXXXX/agent.PID, and a socket's mtime and atime never move after
    it is bound -- which made every login older than the age threshold look like
    abandoned junk. Regular files, directories and symlinks all stay in scope.
    """
    return not (
        stat.S_ISSOCK(mode) or stat.S_ISFIFO(mode) or stat.S_ISBLK(mode) or stat.S_ISCHR(mode)
    )


def _holds_special_file(path: Path) -> bool:
    """Return True when *path* directly contains a socket, FIFO or device node.

    Skipping a socket while still deleting the directory it sits in unlinks the
    socket anyway, so the entry-type rule needs this companion check on the
    directory branch. /tmp/ssh-XXXXXXXX/agent.PID is exactly that shape: one
    directory holding one socket whose timestamps never move once it is bound.
    A directory that cannot be read is kept too -- deleting it blind is the one
    outcome that cannot be undone.
    """
    try:
        with os.scandir(path) as it:
            for entry in it:
                with contextlib.suppress(OSError):
                    if not is_reclaimable_entry_type(entry.stat(follow_symlinks=False).st_mode):
                        return True
    except OSError:
        return True
    return False


def age_cutoff(days: int) -> float:
    """Epoch seconds before which an entry is old enough to be cleaned.

    Every age gate goes through here so config.json's ``min_age_days`` is a real
    floor: it can push a threshold further into the past, never closer to now.
    That direction is deliberate -- a hand-edited config can make topo more
    cautious than its own defaults, but it can never make any cleaner more
    aggressive than the code already is. It reaches the ungated sweeps too
    (``days=0``, a couple of pure-cache directories), which is why the shipped
    default is 0: a floor is something the user asks for, never a default that
    quietly narrows what a cleanup reclaims.
    """
    return time.time() - (max(days, get_min_age_days()) * SECONDS_PER_DAY)


def clean_path_by_age(path: str | Path, days: int, dry_run: bool = False) -> tuple[int, int]:
    """Cleans items within a path that haven't been touched in 'days' days."""
    path = Path(path).expanduser()
    if not path.exists() or not path.is_dir():
        return 0, 0

    total_size = 0
    items_count = 0
    cutoff = age_cutoff(days)

    try:
        with os.scandir(path) as entries:
            for entry in entries:
                try:
                    # lstat() judges the entry itself and never follows a symlink to its
                    # target. Consider both atime and mtime so that 'noatime'/'relatime'
                    # mounts (where atime barely updates) don't make active data look stale.
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_atime >= cutoff or st.st_mtime >= cutoff:
                    continue
                if not is_reclaimable_entry_type(st.st_mode):
                    continue
                item = Path(entry.path)
                if entry.is_dir(follow_symlinks=False):
                    if _holds_special_file(item):
                        continue
                    stats = _get_path_stats(item)
                    if stats is not None:
                        if float(stats.get("newest_activity_secs", 0)) >= cutoff:
                            continue
                        size = _coerce_non_negative_size(stats.get("total_size_bytes")) or 0
                    else:
                        if _has_recent_content(item, cutoff):
                            continue
                        size = get_size_fast(item)
                else:
                    size = st.st_size
                if dry_run:
                    safe_remove(item, use_trash=False, dry_run=True, known_size_bytes=size)
                    total_size += size
                    items_count += 1
                elif safe_remove(item, use_trash=False, known_size_bytes=size)[0]:
                    total_size += size
                    items_count += 1
    except OSError:
        return total_size, items_count
    return total_size, items_count


# One table for both halves of the job: the units the pattern accepts and the
# power of 1024 each one means. They used to be two hand-kept lists that had
# already drifted -- the pattern took E, the multiplier chain stopped at P, so
# "10 EB" parsed as 10 bytes.
_SIZE_UNIT_POWERS = {"K": 1, "M": 2, "G": 3, "T": 4, "P": 5, "E": 6}
_SIZE_PREFIXES = "".join(_SIZE_UNIT_POWERS)
# What makes this safe to point at a line of command output:
#
# (?<![0-9A-Za-z.,]) the number may not start inside a word. A machine-id is one
#     continuous word, so no position in 76223d7d...f06ec503c can begin a match
#     -- that hex string used to yield "06" + "e" = 6 bytes from the journal path
#     journalctl prints before its total.
# [0-9]+(?:,[0-9]{3})* accepts thousands separators, so "1,234 MB" is 1234 MB
#     rather than the 234 MB a match starting after the comma used to report. The
#     groups must be three digits: a de_DE "1,2 GB" is then not read at all,
#     rather than read as 12 GB.
# (?![A-Za-z]) the unit may not be the first letter of a word. This is what keeps
#     the bare-prefix alternative -- there to read systemd's suffix-less "1.1G" --
#     from reading "Removing 12 packages" as 12 PiB.
_SIZE_PATTERN = re.compile(
    rf"(?<![0-9A-Za-z.,])(?P<sign>-)?(?P<value>[0-9]+(?:,[0-9]{{3}})*(?:\.[0-9]+)?)\s*"
    rf"(?P<unit>[{_SIZE_PREFIXES}]i?B|[{_SIZE_PREFIXES}]|B)(?![A-Za-z])",
    re.IGNORECASE,
)


def parse_size_to_bytes(text: str) -> int:
    """Parse a human-readable size string as bytes using binary units.

    Reads the *first* size in the text, which is only ever correct when the text
    is a size and nothing else, or when the caller has already anchored on the
    line it wants (journal_freed_bytes below, _apt_freed_bytes and friends in
    clean/system.py). Fed a whole transcript it answers about whatever token
    happens to come first, which is not the same question as "how much was
    freed".
    """
    if not text or text == "N/A":
        return 0
    match = _SIZE_PATTERN.search(text)
    if match:
        if match.group("sign"):
            # No cleanup command reports freeing a negative number of bytes, so
            # this is a misread rather than a measurement -- and reading it as a
            # positive size, which dropping the sign used to do, is the worst of
            # the available answers.
            return 0
        try:
            # The pattern admits only digits and three-digit groups, so float()
            # cannot fail on the value itself. 309 digits or more of them become
            # inf, and int() refuses to convert that.
            return int(
                float(match.group("value").replace(",", ""))
                * 1024 ** _SIZE_UNIT_POWERS.get(match.group("unit")[0].upper(), 0)
            )
        except OverflowError:
            return 0
    # A bare numeric string (no unit) is treated as raw bytes — but only when the
    # whole value is numeric, so stray digits in command output aren't misread.
    stripped = text.strip()
    if stripped and stripped.replace(".", "", 1).isdigit():
        try:
            return int(float(stripped))
        except ValueError:
            return 0
    return 0


# journalctl's own total, one line per journal directory it vacuumed:
# "Vacuuming done, freed 1.1G of archived journals from /var/log/journal/<id>."
# Anchored on that sentence because the "Deleted archived journal <path> (128.0M)"
# lines above it come first and carry both a per-file size and the machine-id
# whose hex digits parse_size_to_bytes used to read as a size of its own.
# systemd formats these with FORMAT_BYTES: 1024-based, and with no B after the
# prefix ("1.1G"), which is why the bare-prefix unit exists at all.
_JOURNAL_VACUUM_FREED = re.compile(r"freed\s+(\S+)\s+of\s+archived\s+journals", re.IGNORECASE)


def journal_freed_bytes(output: str) -> int:
    """Bytes journalctl says its vacuum freed, 0 when it did not say.

    Summed rather than taken from one line: journal_directory_vacuum() reports
    per directory, so a machine with both /var/log/journal and /run/log/journal
    prints two totals and the freed space is their sum.
    """
    return sum(parse_size_to_bytes(size) for size in _JOURNAL_VACUUM_FREED.findall(output))


# Alias for semantic clarity in command output parsing
parse_size_from_text = parse_size_to_bytes
