"""How topo's own JSON state is read and written: one reader, one writer.

The whitelist and the config used to do both by hand, and both got it wrong in
the same two ways.

Writing was ``open(path, "w")`` followed by ``json.dump``. That truncates the
file before the first byte of the replacement is written, so anything that
interrupts the dump -- a full disk, a killed process, a power cut -- leaves a
half-written or empty file where the old, intact one used to be. For the
whitelist that is not a lost setting but a lost *protection*: every path the user
added by hand, gone, and the reader below used to report the wreckage as an empty
list, which reads as "nothing is protected".

Reading was ``open(path)`` plus ``json.load``, guarded by
``except (OSError, json.JSONDecodeError)``. A file whose bytes are not UTF-8
raises UnicodeDecodeError instead -- a ValueError, so neither of those clauses
catches it and the whole command ends in a traceback. Decoding with
``errors="replace"`` here means malformed bytes arrive as a JSON syntax error,
which is the one thing every caller already knows how to answer.

The reader deliberately returns a *state* alongside the value, because "the file
is not there" and "the file is there and I cannot trust it" are different
questions and the callers answer them differently: a missing config means take
the defaults, a missing whitelist means the user has added nothing yet, but an
unreadable whitelist must never be mistaken for either.
"""

import contextlib
import itertools
import json
import os
import stat
from pathlib import Path
from typing import Any, Literal

# ok         parsed; the value is whatever the file held
# missing    no such file -- the caller's own default applies
# unreadable present but not usable: bad JSON, bad bytes, no permission, a
#            directory. Never silently equivalent to "missing".
JsonState = Literal["ok", "missing", "unreadable"]


def read_json(path: Path) -> tuple[Any, JsonState]:
    """Parse *path*, reporting whether it was absent or merely unusable.

    Never creates anything: every command reads the config before printing its
    first line, and a read that wrote would have `topo remove` create the file it
    then reports as leftover configuration.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "missing"
    except OSError:
        return None, "unreadable"

    try:
        # Decode before parsing rather than handing json the bytes: json.loads
        # would decode them strictly and raise UnicodeDecodeError, which is not a
        # JSONDecodeError and so escapes every caller's except clause.
        return json.loads(raw.decode("utf-8", errors="replace")), "ok"
    except (ValueError, RecursionError):
        # JSONDecodeError is a ValueError, so naming both would only suggest they
        # are different cases. RecursionError is not one: deeply nested arrays hit
        # the interpreter's limit inside the scanner, which is still just a file
        # this module could not read.
        return None, "unreadable"


# A process-wide counter so no two writes share a scratch name. next() on an
# itertools.count is atomic under the GIL, so it hands a fresh value to every
# concurrent caller without a lock of its own.
_scratch_counter = itertools.count()


def write_json_atomic(path: Path, data: Any, *, mode: int = 0o644) -> bool:
    """Replace *path* with *data*, or leave it exactly as it was.

    The temporary file is a sibling, so os.replace() stays within one filesystem
    and is therefore atomic: a reader sees either the old file or the new one,
    never a truncated one. fsync before the rename is what makes that true after
    a crash as well -- without it the rename can land while the new contents are
    still only in the page cache, which is the empty-file failure in a slower
    disguise. The scratch name is unique per *write*, not just per process: the
    pid keeps two processes -- and a crashed run's leftovers -- from sharing one
    file, but two threads of one process share a pid, so a pid-only name let them
    truncate the same scratch file and one thread's os.replace() then published
    what the other half-wrote (or failed ENOENT once the sibling renamed it
    away). The counter closes that window -- the whitelist seed racing eight
    `topo whitelist add` threads each get their own scratch file -- so the
    atomicity guarantee holds within a process too.

    *mode* is the permission bits the final file ends up with (subject to the
    umask, which can only tighten it). It defaults to 0644 -- the plain
    ``open(..., "w")`` default this used to produce -- so a caller that does not
    care keeps the old behaviour. The whitelist and config pass 0600: the
    whitelist in particular is the one file whose contents say which paths the
    user cares about, and leaving it world-readable is the asymmetry this closes.
    The scratch file is opened with O_EXCL and O_NOFOLLOW rather than a bare
    ``open``: the name is unique per write, so one that already exists is a
    planted file or a symlink left in our place, and following it would write the
    new contents somewhere we did not choose -- refusing is the safe answer.
    """
    tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{next(_scratch_counter)}")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return False

    # Durability of the rename itself, not of the contents. Best-effort: some
    # filesystems refuse to open a directory for fsync, and a lost rename leaves
    # the previous intact file in place -- which is the safe side to fail on.
    with contextlib.suppress(OSError):
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    return True


def ensure_private_dir(path: Path) -> bool:
    """Create *path* as a 0700 directory, tightening it if it already exists.

    mkdir's ``mode`` applies only at creation, and ``exist_ok=True`` ignores it
    outright, so a directory an older release (or another tool) first created
    group- or world-readable stays that way until something actively chmods it --
    the same reason the audit log reclaims its state dir to 0700 in file_ops. The
    config dir holds the whitelist, so it is brought to 0700 here. Only a real
    directory we own is tightened: a symlink is never chmod'd through, and under
    sudo a dir owned by someone else is left alone rather than locked away from
    its owner. Best-effort on the chmod -- a mode we may not change leaves the
    looser one rather than failing the write, which is the safe side to err on.
    """
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        return False
    with contextlib.suppress(OSError):
        st = path.lstat()
        if (
            stat.S_ISDIR(st.st_mode)
            and st.st_uid == os.getuid()
            and stat.S_IMODE(st.st_mode) & 0o077
        ):
            os.chmod(path, 0o700)
    return True
