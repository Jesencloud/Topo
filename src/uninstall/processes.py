"""Closing an app's processes, the step that has to precede its removal.

A package manager will happily unlink the binary of a running program, and the
process keeps going on the inode it already holds -- writing its config back out
on exit, over residue that has just been deleted, or crashing in a way the user
reads as topo's fault. So every removal path closes the app first, and this
module is the whole of that: which names could be the app's processes, and how
they are signalled.

The escalation is SIGTERM, wait, SIGKILL the survivors, wait -- and the two waits
are per call rather than per app, which is why there are two entry points rather
than one. :func:`terminate_apps` closes an entire selection inside a single 1.5 s
window; :func:`terminate_app_processes` closes one app, and normally finds
nothing left to do because the batch call already ran.

Split out of ``UninstallManager`` because none of it touched the class: the four
functions here talk to /proc and pkill, share no state, and the only thing they
needed from their old home was :func:`~src.uninstall.names.name_matches`, which
is now next door. Nothing here may import ``manager.py`` -- everything that
removes files calls in this direction, never back.
"""

import contextlib
import re
import subprocess
import time
from pathlib import Path

from ..core import system
from ..core.constants import AppType
from ..core.desktop_entry import get_desktop_exec_names
from ..core.file_ops import comm_pattern, running_process_comms
from .discovery import AppRecord
from .names import name_matches

# How long a process gets to act on SIGTERM before it is killed, and how long the
# kernel gets to reap it afterwards. Both are waited through once per selection,
# not once per app.
SIGTERM_GRACE_SECONDS = 1.0
SIGKILL_GRACE_SECONDS = 0.5


def candidate_process_names(app: AppRecord, paths: list[Path] | None = None) -> list[str]:
    """Plausible process (comm) names to terminate before removing an app.

    Dynamically discovers process names using:
    1. Package/Flatpak/Snap ID
    2. Binary names parsed from all associated .desktop Exec fields
    3. Active PIDs occupying the app's residue directories via fuser
    4. Name tokens (splitting hyphens/underscores/prefixes)
    """
    names: set[str] = set()
    app_id = str(app.get("id") or "")
    app_name = str(app.get("name") or "")

    if app_id:
        names.add(app_id)
        names.add(app_id.lower())
        if "." in app_id:  # flatpak: org.gnome.Music -> music
            names.add(app_id.rsplit(".", 1)[-1].lower())

    # Generic token splitting: e.g. "google-chrome-stable" -> "chrome", "linuxqq" -> "qq"
    for source_name in (app_id, app_name):
        if not source_name or " " in source_name:
            continue
        lowered = source_name.lower()
        for prefix in ("linux", "org.", "com.", "net.", "io.", "io.github."):
            if lowered.startswith(prefix) and len(lowered) > len(prefix) + 2:
                names.add(lowered[len(prefix) :])
        for part in lowered.replace("_", "-").split("-"):
            if len(part) >= 3 and part not in (
                "stable",
                "beta",
                "dev",
                "desktop",
                "linux",
                "free",
                "community",
            ):
                names.add(part)

    # Dynamic .desktop Exec binary extraction
    desktop_dirs = [
        Path("/usr/share/applications"),
        Path.home() / ".local/share/applications",
        Path("/var/lib/flatpak/exports/share/applications"),
        Path.home() / ".local/share/flatpak/exports/share/applications",
    ]
    targets = {name for name in (app_id.lower(), app_name.lower()) if name}
    for desktop_dir in desktop_dirs:
        if not desktop_dir.is_dir():
            continue
        with contextlib.suppress(OSError):
            for entry in desktop_dir.glob("*.desktop"):
                # Everything collected here ends up as an argument to `pkill -9`,
                # so the match has to be as strict as the one guarding residue
                # deletion: a bare substring test would let a two-letter id like
                # "go" or "qq" pull in half of /usr/share/applications and kill
                # whatever those entries happen to run. A file named exactly after
                # the app is still taken, even for a token name_matches rejects as
                # generic -- go.desktop is unambiguously the entry for id "go".
                stem = entry.stem.lower()
                # Reverse-DNS entries carry the app's own name last:
                # org.gnome.Music.desktop for org.gnome.Music.
                entry_names = {stem, stem.rsplit(".", 1)[-1]}
                if stem in targets or any(
                    name_matches(entry_name, target)
                    for entry_name in entry_names
                    for target in targets
                ):
                    names.update(get_desktop_exec_names(entry))

    # Dynamic fuser / lsof inspection on app residue paths
    if paths:
        for residue_path in paths:
            if residue_path.exists():
                try:
                    fuser = system.run_command(
                        ["fuser", str(residue_path)], capture=True, timeout=3
                    )
                    stdout_text = str(fuser.stdout or "")
                    if fuser.ok and stdout_text.strip():
                        # fuser outputs PIDs like '1234m'; extract pure numeric PIDs
                        for pid_clean in re.findall(r"\b\d+\b", stdout_text):
                            comm_path = Path(f"/proc/{pid_clean}/comm")
                            if comm_path.exists():
                                with contextlib.suppress(OSError):
                                    # prctl() lets a process name itself with
                                    # any 15 bytes; suppress(OSError) does not
                                    # catch the UnicodeDecodeError a strict
                                    # decode would raise on them.
                                    comm_name = comm_path.read_text(errors="replace").strip()
                                    if comm_name:
                                        names.add(comm_name)
                except (OSError, subprocess.SubprocessError):
                    pass

    return [name for name in names if name]


def _terminate_process_patterns(patterns: list[str]) -> None:
    """SIGTERM the given comm patterns, wait once, then SIGKILL the survivors.

    The two waits are per call, not per pattern, so the caller decides how
    often they are paid: terminate_apps closes a whole selection in one 1.5 s
    window, where a per-app kill spent that on every app in turn.
    """
    if not patterns:
        return
    for pattern in patterns:
        system.run_command(["pkill", "-15", "-x", pattern], capture=True, timeout=5)

    time.sleep(SIGTERM_GRACE_SECONDS)

    # One more /proc pass tells us who ignored SIGTERM; the alternative is a
    # `pgrep -x` per pattern.
    survivors = running_process_comms()
    killed = False
    for pattern in patterns:
        if pattern in survivors:
            system.run_command(["pkill", "-9", "-x", pattern], capture=True, timeout=5)
            killed = True
    if killed:
        time.sleep(SIGKILL_GRACE_SECONDS)


def terminate_apps(targets: list[tuple[AppRecord, list[Path], bool]]) -> None:
    """Close every selected app's processes before the removals start.

    execute_uninstall still does this for its own app, so this function is an
    optimisation rather than a prerequisite: doing it for the whole selection
    at once means the SIGTERM grace period is waited through once instead of
    once per app, and the per-app step then finds nothing left to kill and
    waits not at all. Ten apps used to spend fifteen seconds here.
    """
    running = running_process_comms()
    patterns: list[str] = []
    for app, paths, _ in targets:
        if app.get("type") == AppType.FLATPAK:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                system.run_command(["flatpak", "kill", str(app["id"])], capture=True, timeout=20)
        for proc in candidate_process_names(app, paths):
            pattern = comm_pattern(proc)
            if pattern in running and pattern not in patterns:
                patterns.append(pattern)
    _terminate_process_patterns(patterns)


def terminate_app_processes(app: AppRecord, paths: list[Path]) -> None:
    """Close one app's processes, the step that has to precede its removal.

    terminate_apps applies the same policy to a whole selection and has
    normally already run by the time this does, which is what makes this
    cheap rather than redundant: the /proc pass finds nothing left and
    _terminate_process_patterns returns without waiting out a grace period.

    The two are near-twins on purpose. This one runs `flatpak kill` before
    taking the /proc snapshot and the batch one after, so an app that flatpak
    has already stopped costs a pkill and a 1.5 s wait there and nothing
    here. Merging them means choosing one of those two behaviours for both
    callers -- a decision to make deliberately, not while moving code.
    """
    # Use real executable names (id + .desktop Exec), never the localized
    # display name.
    all_process_names = candidate_process_names(app, paths)
    if app["type"] == AppType.FLATPAK:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            system.run_command(["flatpak", "kill", app["id"]], capture=True, timeout=20)

    # Patterns go through comm_pattern so a long executable name still
    # matches -- and still gets signalled. Which of them are actually
    # running is one /proc read for all of them; when terminate_apps has
    # already closed the selection this list comes back empty and the grace
    # periods are skipped entirely.
    running = running_process_comms()
    processes_to_kill: list[str] = []
    for proc in all_process_names:
        pattern = comm_pattern(proc)
        if pattern in running and pattern not in processes_to_kill:
            processes_to_kill.append(pattern)

    _terminate_process_patterns(processes_to_kill)
