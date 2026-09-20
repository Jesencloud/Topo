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
from collections.abc import Iterable
from pathlib import Path

from ..core import system
from ..core.constants import AppType
from ..core.desktop_entry import get_desktop_exec_names
from ..core.file_ops import (
    comm_pattern,
    process_cgroup,
    process_exe_path,
    running_process_comms,
)
from .discovery import AppRecord
from .names import name_matches

# How long a process gets to act on SIGTERM before it is killed, and how long the
# kernel gets to reap it afterwards. Both are waited through once per selection,
# not once per app.
SIGTERM_GRACE_SECONDS = 1.0
SIGKILL_GRACE_SECONDS = 0.5


def candidate_process_names(app: AppRecord) -> list[str]:
    """Plausible process (comm) names to terminate before removing an app.

    Every name here is derived from the app's own identity -- its id, the
    de-prefixed and hyphen-split forms of that, and the binary names in its
    .desktop Exec fields -- and each is filtered through ``name_matches``, which
    drops the short generic tokens (``go``, ``qq``) that would otherwise match
    half the process table. That filter is what makes these names safe to hand
    to ``pkill -x``: they cannot be a bystander's name.

    What is *not* here: the processes merely holding an app's residue directory
    open. Those are found by PID via fuser (:func:`app_owned_pids_in_paths`) and
    signalled by PID, never folded into this name list -- a shell or editor
    sitting in ``~/.config/foo`` has a comm of ``bash`` or ``nvim``, and turning
    that into a ``pkill -9 -x bash`` would kill it system-wide.

    Discovers names using:
    1. Package/Flatpak/Snap ID
    2. Binary names parsed from all associated .desktop Exec fields
    3. Name tokens (splitting hyphens/underscores/prefixes)
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

    return [name for name in names if name]


def _pid_belongs_to_app(pid: int, app: AppRecord) -> bool:
    """Whether *pid* can be proven to be one of *app*'s own processes.

    Proof, not guess: a PID found holding an app's residue directory open might
    be the app itself, or it might be a shell, editor or file manager the user
    left sitting there. Only the first may be killed, so this returns True only
    when something ties the process to the app, and False -- skip it -- whenever
    it cannot be read or cannot be tied. Any one signal is enough.

    - exe basename matches one of the app's own identity names. Those names come
      from ``candidate_process_names`` (id, de-prefixed forms, hyphen tokens,
      .desktop Exec names), already filtered through ``name_matches``, so a
      bystander's ``bash`` or ``nvim`` matches none of them.
    - a standalone CLI tool's binary lives under its recorded install_dir.
    - a Flatpak's cgroup names its application id (``app-flatpak-<id>-*.scope``).
    - a Snap's cgroup carries the ``snap.<name>.`` slice prefix.
    """
    exe = process_exe_path(pid)
    if exe is not None:
        exe_name = exe.name.lower()
        if any(name_matches(exe_name, token.lower()) for token in candidate_process_names(app)):
            return True
        if app.get("type") == AppType.CLI and app.get("install_dir"):
            install_dir = Path(app["install_dir"])
            with contextlib.suppress(OSError):
                resolved = exe.resolve()
                if resolved == install_dir or install_dir in resolved.parents:
                    return True

    app_id = str(app.get("id") or "")
    if app_id and app.get("type") in (AppType.FLATPAK, AppType.SNAP):
        cgroup = process_cgroup(pid)
        if app.get("type") == AppType.FLATPAK and app_id in cgroup:
            return True
        if app.get("type") == AppType.SNAP and f"snap.{app_id}." in cgroup:
            return True

    return False


def app_owned_pids_in_paths(app: AppRecord, paths: list[Path]) -> set[int]:
    """PIDs holding *app*'s residue paths open that are provably *app*'s own.

    fuser answers "who has this path open" with a list of PIDs; each is kept
    only if :func:`_pid_belongs_to_app` can tie it to the app. The bystander
    PIDs -- a terminal cd'd into the directory, an editor with a file in it --
    are dropped here and never signalled. The old code turned these same PIDs
    into their comm names and fed them to ``pkill -9 -x``, which killed every
    process on the machine that happened to share a name.
    """
    owned: set[int] = set()
    for residue_path in paths:
        if not residue_path.exists():
            continue
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            fuser = system.run_command(["fuser", str(residue_path)], capture=True, timeout=3)
            stdout_text = str(fuser.stdout or "")
            if fuser.ok and stdout_text.strip():
                # fuser outputs PIDs like '1234m'; extract pure numeric PIDs.
                for pid_str in re.findall(r"\b\d+\b", stdout_text):
                    pid = int(pid_str)
                    if pid not in owned and _pid_belongs_to_app(pid, app):
                        owned.add(pid)
    return owned


def _signal_pids(pids: Iterable[int], signal: str) -> None:
    """Send one signal to each PID by number, through the same run_command path.

    kill by PID, not pkill by name: these PIDs are already proven to be the
    app's, so signalling the exact process is both precise and the only thing
    that lets survivorship be judged per PID afterwards.
    """
    for pid in pids:
        system.run_command(["kill", f"-{signal}", str(pid)], capture=True, timeout=5)


def _pid_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def _terminate_pids(pids: set[int]) -> set[int]:
    """SIGTERM these PIDs, wait, SIGKILL the survivors, wait, return who is left.

    Existence under /proc is the survivorship test, deliberately not
    ``running_process_comms``: these are known PIDs, so one stat each answers
    the question, and terminate_apps must not spend one of that call's mocked
    passes here. The returned set is the app's own processes that ignored even
    SIGKILL -- the signal to abort the removal rather than delete files under a
    live process.
    """
    if not pids:
        return set()
    _signal_pids(pids, "TERM")
    time.sleep(SIGTERM_GRACE_SECONDS)
    survivors = {pid for pid in pids if _pid_alive(pid)}
    if survivors:
        _signal_pids(survivors, "KILL")
        time.sleep(SIGKILL_GRACE_SECONDS)
        survivors = {pid for pid in survivors if _pid_alive(pid)}
    return survivors


def _describe_pid(pid: int) -> str:
    """A short label for a surviving PID, for the removal report only."""
    exe = process_exe_path(pid)
    return f"{exe.name if exe else '?'} (pid {pid})"


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
    owned_pids: set[int] = set()
    for app, paths, _ in targets:
        if app.get("type") == AppType.FLATPAK:
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                system.run_command(["flatpak", "kill", str(app["id"])], capture=True, timeout=20)
        for proc in candidate_process_names(app):
            pattern = comm_pattern(proc)
            if pattern in running and pattern not in patterns:
                patterns.append(pattern)
        owned_pids |= app_owned_pids_in_paths(app, paths)
    _terminate_process_patterns(patterns)
    # The per-app step is the authoritative survivor gate; this batch pass only
    # clears the field ahead of it, so its return is not consulted here.
    _terminate_pids(owned_pids)


def terminate_app_processes(app: AppRecord, paths: list[Path]) -> list[str]:
    """Close one app's processes; return the ones proven to survive it.

    The step that has to precede a removal, and the authoritative one: the
    caller reads its return to decide whether deleting the app's files now would
    be deleting them under a live process. An empty list means the field is
    clear.

    terminate_apps applies the same policy to a whole selection and has normally
    already run by the time this does, which is what makes this cheap rather
    than redundant: the /proc pass finds nothing left and the grace periods are
    skipped entirely.

    Two kinds of process, two treatments. The name-heuristic patterns go to
    ``pkill -x`` and their survivorship is not reported: a comm still running
    after the kill may be an unrelated process that shares the name, and it must
    not block this app's removal. The PIDs fuser proved to be the app's own are
    signalled by number, and those that outlive even SIGKILL come back here --
    they are the app, still running, and the removal must not proceed over them.
    """
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
    for proc in candidate_process_names(app):
        pattern = comm_pattern(proc)
        if pattern in running and pattern not in processes_to_kill:
            processes_to_kill.append(pattern)
    _terminate_process_patterns(processes_to_kill)

    survivors = _terminate_pids(app_owned_pids_in_paths(app, paths))
    return [_describe_pid(pid) for pid in sorted(survivors)]
