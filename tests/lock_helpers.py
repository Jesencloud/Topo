"""Shared scaffolding for tests that need a lock held by a *different* process.

POSIX record locks (``fcntl.lockf`` / ``F_SETLK``) are owned by the process and
``flock`` locks by the open file description, so a lock taken inside the test
process is invisible to a probe made from that same process. Every "is it
locked?" assertion therefore needs a real external holder.
"""

import contextlib
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

# Holds an exclusive POSIX record lock — the space SQLite, Chrome and Firefox use.
RECORD_LOCK_HOLDER = """
import fcntl, sys
f = open(sys.argv[1], "r+")
fcntl.lockf(f.fileno(), fcntl.LOCK_EX)
sys.stdout.write("ready\\n")
sys.stdout.flush()
sys.stdin.readline()
"""


@contextlib.contextmanager
def external_holder(script: str, target: Path) -> Iterator[subprocess.Popen]:
    """Run *script* in a child process that holds a lock on *target* until exit."""
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(target)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        assert proc.stdout.readline().strip() == "ready"
        yield proc
    finally:
        with contextlib.suppress(Exception):
            assert proc.stdin is not None
            proc.stdin.write("go\n")
            proc.stdin.flush()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=10)
