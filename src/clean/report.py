from ..core.constants import FAIL
from ..core.system import CommandResult

# Count of command-level failures in the current `topo clean` run. A cleaner
# that runs an external command to do real work (apt-get clean, a kernel purge, a
# docker prune) and gets a non-zero result reports it here; run_clean resets this
# at the start of each run and reads it at the end to decide its exit status. It
# is deliberately NOT bumped by the per-file occupancy failures the user-data and
# app sweeps expect (a cache file another process still holds) -- those are not a
# reason for `topo clean` to tell a script the cleanup failed. The clean pipeline
# is single-threaded (unlike optimize), so a plain module counter needs no lock.
_command_failures = 0


def reset_command_failures() -> None:
    """Zero the failure counter at the start of a `topo clean` run."""
    global _command_failures
    _command_failures = 0


def command_failure_count() -> int:
    """How many command-level failures report_command_failure has recorded."""
    return _command_failures


def report_command_failure(task: str, result: CommandResult) -> None:
    """Print one line so a failed command reads differently from nothing to do.

    Every cleaner in this package returns a size/items tuple, and a genuine
    command failure used to hand back the same all-zero tuple as "there was
    nothing to clean" -- so on screen a broken `apt-get clean` (dpkg lock held,
    disk full, no permission) was indistinguishable from a tidy system. This
    prints the distinguishing line, carrying the first non-blank line of the
    command's own stderr when it wrote one -- and the internal error text
    (timeout, spawn failure) when it did not -- so the reason travels with it.

    It also bumps the run's failure counter: the printed line made the failure
    visible on screen, and the count makes it visible in the exit status, so
    `topo clean` in a script or CI can tell a partial failure from a clean run
    that had nothing to do. The two must stay together -- every place that tells
    the user a command failed is a place a script must be able to detect.
    """
    global _command_failures
    _command_failures += 1
    detail = next((line.strip() for line in result.stderr.splitlines() if line.strip()), "")
    if not detail:
        detail = result.error
    suffix = f": {detail}" if detail else ""
    print(f"  {FAIL} {task} failed{suffix}")
