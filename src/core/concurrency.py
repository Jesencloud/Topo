"""How many threads topo runs I/O-bound work on, decided in one place.

Five call sites used to carry five unrelated numbers -- 1, 2, 4, 8, and "one per
task" -- and nothing in the program ever asked the machine how many CPUs it had:
``os.cpu_count()`` appeared once, in status.py, to *display* the core count. So a
128-core build host and a single-core container ran the same fan-out, one call
site opened a thread per task with no ceiling at all, and a user whose $HOME sits
on NFS had no way to turn any of it down.

Two things are deliberately kept apart here.

*Scan* work forks topo-core, and the engine parallelises its own walk across the
machine's cores. So N scan threads mean N x cores concurrent ``readdir`` calls on
one filesystem, which is why the scan cap is small and stays small: the
parallelism that matters for a scan already lives inside the engine, and piling
Python threads on top of it mostly multiplies seek pressure. (Handing the engine
its own thread count is the other half of this, and it needs a change inside
topo-core -- see PERFORMANCE_LARGE_FS_REVIEW_2026-10-07.md.)

*Probe* work is short and syscall-bound -- stat a path, ask a package manager
whether it owns a file -- and benefits from more concurrency than a scan, but
still saturates early.

``TOPO_IO_THREADS`` is the knob the review asked for: a ceiling the user sets
once, honoured by every pool here. Someone on NFS, sshfs or CIFS can pin it to 1
and get serial behaviour out of every fan-out in the program.
"""

import os

#: Environment variable capping every pool below. Unset, or unparseable, means
#: "use the built-in caps".
MAX_WORKERS_ENV = "TOPO_IO_THREADS"

#: Environment variable the Rust engine reads to size its own walk. Set by the
#: user, or derived from MAX_WORKERS_ENV below; see engine_env().
SCAN_THREADS_ENV = "TOPO_SCAN_THREADS"

#: Scans fork the engine, which is itself parallel -- see the module docstring.
SCAN_WORKER_CAP = 2

#: Short filesystem/command probes. Eight was the number two uninstall pools
#: already used; it is kept, now as a cap rather than a constant, so the pool
#: cannot exceed the task count, the CPU count, or the user's ceiling.
PROBE_WORKER_CAP = 8


def worker_ceiling() -> int | None:
    """The user's ``TOPO_IO_THREADS`` ceiling, or None when they set none.

    A value below 1 is read as 1 rather than rejected: "no concurrency" is a
    thing someone may reasonably ask for, and a pool of zero threads is not.
    Anything non-numeric is ignored, because failing a cleanup over a typo in an
    environment variable would be worse than running at the default width.
    """
    raw = os.environ.get(MAX_WORKERS_ENV, "").strip()
    if not raw:
        return None
    try:
        return max(1, int(raw))
    except ValueError:
        return None


def workers(task_count: int, *, cap: int) -> int:
    """Threads to run *task_count* independent I/O tasks on, never above *cap*.

    Always at least 1 (ThreadPoolExecutor rejects 0) and never more than there is
    work for, so a one-task batch does not stand up a pool of eight.
    """
    limit = min(cap, max(1, task_count))
    cpus = os.cpu_count() or 1
    limit = min(limit, max(1, cpus))
    ceiling = worker_ceiling()
    if ceiling is not None:
        limit = min(limit, ceiling)
    return max(1, limit)


def engine_env() -> dict[str, str]:
    """Environment overlay for a topo-core invocation, usually empty.

    The engine sizes its own walk from ``TOPO_SCAN_THREADS``. Three cases:

    * the user set it -- nothing to do, the child inherits it as written, and
      being explicit about the engine beats anything inferred here;
    * the user set only ``TOPO_IO_THREADS`` -- that ceiling is passed along.
      Someone pinning topo's concurrency to 1 because $HOME is on NFS means the
      engine too, and the engine is where most of the concurrency actually lives:
      every scan thread forks one, and each spreads its walk over all cores;
    * neither is set -- an empty overlay, leaving jwalk's own default (one thread
      per logical CPU). No default is invented here, because which width wins on
      a given disk is a question for a benchmark, not for a guess.
    """
    if os.environ.get(SCAN_THREADS_ENV, "").strip():
        return {}
    ceiling = worker_ceiling()
    if ceiling is None:
        return {}
    return {SCAN_THREADS_ENV: str(ceiling)}
