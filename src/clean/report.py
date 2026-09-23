from ..core.constants import FAIL
from ..core.system import CommandResult


def report_command_failure(task: str, result: CommandResult) -> None:
    """Print one line so a failed command reads differently from nothing to do.

    Every cleaner in this package returns a size/items tuple, and a genuine
    command failure used to hand back the same all-zero tuple as "there was
    nothing to clean" -- so on screen a broken `apt-get clean` (dpkg lock held,
    disk full, no permission) was indistinguishable from a tidy system. This
    prints the distinguishing line, carrying the first non-blank line of the
    command's own stderr when it wrote one -- and the internal error text
    (timeout, spawn failure) when it did not -- so the reason travels with it.
    """
    detail = next((line.strip() for line in result.stderr.splitlines() if line.strip()), "")
    if not detail:
        detail = result.error
    suffix = f": {detail}" if detail else ""
    print(f"  {FAIL} {task} failed{suffix}")
