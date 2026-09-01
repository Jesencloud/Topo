"""Removing one app: its processes, then its package, then its data.

Everything in the uninstall flow that deletes is here, which is the point of the
split rather than a side effect of it. The modules upstream only find things --
``discovery.py`` asks eight package managers what is installed, ``residue.py``
guesses which per-user directories belong to an app -- and finding is allowed to
be heuristic and generous. Acting on the guess is not, so the acting half is one
file, with its guard rails written next to the deletions they cover: residue goes
to the trash unless config.json asked otherwise, a sandbox's single data
directory goes to the trash even then, and nothing is deleted at all until the
package manager has confirmed the app itself is gone.

The order in :func:`execute_uninstall` is the design: close the processes, remove
the package, remove the residue -- and the last step only if the one before it
succeeded.

Split out of ``UninstallManager`` because none of it read the instance: eight
package-manager branches, a trash policy, and the audit and history bookkeeping
wrapped around them, none of which wants the scan cache or the residue index the
class holds. Nothing here may import ``manager.py`` -- the screen composes the
two halves, and the arrow runs scan -> remove, never back.
"""

import contextlib
import shutil
from pathlib import Path

from ..core import system
from ..core.config import get_use_trash
from ..core.constants import AppType
from ..core.file_ops import record_deletion_audit, safe_remove
from ..core.history import record_history_session
from ..core.package_manager import DNF, resolve_admin_tool
from . import processes
from .discovery import AppRecord


def _flatpak_scope(app: AppRecord) -> str:
    """Which installation this Flatpak lives in, or "" when the scan could not tell.

    `flatpak list --columns=installation` prints "system", "user", or the id
    of a custom installation. The third answer normalises to "": its
    ownership is whatever the admin who created that installation decided,
    so the removal is left exactly as it was before any of this.
    """
    scope = str(app.get("flatpak_scope") or "").strip().lower()
    return scope if scope in ("system", "user") else ""


def flatpak_removal_needs_sudo(app: AppRecord) -> bool:
    """Whether removing this Flatpak has to be root's work.

    A system-wide installation lives under /var/lib/flatpak, which the
    invoking user cannot write; flatpak falls back to asking polkit, and a
    session with no polkit agent -- ssh, a bare tty -- simply fails there.
    The screen calls this to decide whether to take a sudo session before it
    enters raw mode, and execute_uninstall calls it to build the command, so
    the authorization and the command that needs it cannot disagree.
    """
    return app.get("type") == AppType.FLATPAK and _flatpak_scope(app) == "system"


def _remove_package(app: AppRecord) -> system.CommandResult:
    """Run the one removal command this app's package manager needs.

    Eight package types plus an explicit refusal, and the branches are long
    because of the flags in them rather than the calls: apt-get instead of
    apt, zypper's --clean-deps, the Flatpak scope. Kept out of
    execute_uninstall so that its own subject -- the audit and history
    bookkeeping wrapped around this one call -- is not read through a hundred
    lines of package manager detail.

    Returns a CommandResult even where nothing is spawned (CLI, unsupported),
    because the caller's only question is whether the removal succeeded.
    """
    if app["type"] == AppType.FLATPAK:
        # The scope is not what makes the app findable -- `flatpak
        # uninstall` searches both installations to resolve a ref -- it
        # decides which copy goes when the same ref is installed in
        # both, and it names the installation the sudo decision was
        # made for.
        scope = _flatpak_scope(app)
        flatpak_cmd = ["flatpak", "uninstall"]
        if scope:
            flatpak_cmd.append(f"--{scope}")
        flatpak_cmd += ["-y", app["id"]]
        return system.run_command(
            flatpak_cmd,
            use_sudo=flatpak_removal_needs_sudo(app),
            capture=True,
            timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
        )

    if app["type"] == AppType.SNAP:
        return system.run_command(
            ["snap", "remove", "--purge", app["id"]],
            use_sudo=True,
            capture=True,
            timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
        )

    if app["type"] == AppType.NPM:
        result = system.run_command(["npm", "uninstall", "-g", app["id"]], capture=True, timeout=60)
        _prune_empty_npm_scope_dir(app["id"])
        return result

    if app["type"] == AppType.CLI:
        # Remove standalone binary & install directory
        home_path = Path.home()
        cli_targets = [
            home_path / ".local/bin" / app["id"],
            home_path / ".local/share" / app["id"],
            home_path / f".{app['id']}",
        ]
        for cli_target in cli_targets:
            if cli_target.exists():
                safe_remove(cli_target, use_trash=get_use_trash(), allow_app_data_removal=True)
        return system.CommandResult(args=["cli_uninstall"], returncode=0, stdout="CLI uninstalled")

    if app["type"] == AppType.APT:
        # apt-get, not apt: apt prints "WARNING: apt does not have a stable
        # CLI interface" when its output is captured, and the rest of the
        # repository already standardises on apt-get.
        #
        # --autoremove for the same reason the zypper branch passes
        # --clean-deps, and with exactly the flags collateral_packages()
        # simulated: the orphans this removal creates go with it, and they
        # are the ones the preview listed. The screen used to follow the
        # whole selection with a single system-wide `apt-get autoremove
        # --purge -y` instead, one transaction that took every unused
        # auto-installed package on the box -- none of them previewed, and
        # not only the ones this app had pulled in.
        return system.run_command(
            ["apt-get", "purge", "--autoremove", "-y", app["id"]],
            use_sudo=True,
            capture=True,
            env=system.APT_NONINTERACTIVE_ENV,
            timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
        )

    if app["type"] == AppType.PACMAN:
        return system.run_command(
            ["pacman", "-Rns", "--noconfirm", app["id"]],
            use_sudo=True,
            capture=True,
            timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
        )

    if app["type"] == AppType.ZYPPER:
        return system.run_command(
            # --clean-deps for the same reason the apt branch above passes
            # --autoremove: dnf drops the dependencies nothing needs any
            # more by default and pacman is asked to with -Rns, while
            # zypper keeps them unless told, which would leave openSUSE the
            # one family where uninstalling quietly leaves orphans on disk.
            ["zypper", "--non-interactive", "remove", "--clean-deps", app["id"]],
            use_sudo=True,
            capture=True,
            env=system.C_LOCALE_ENV,
            timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
        )

    if app["type"] == AppType.DNF:
        dnf_cmd = resolve_admin_tool(DNF)
        return system.run_command(
            [dnf_cmd, "remove", "-y", app["id"]],
            use_sudo=True,
            capture=True,
            timeout=system.PACKAGE_TRANSACTION_TIMEOUT,
        )

    # Named explicitly rather than fallen through to: the old else ran
    # `dnf remove` for anything it did not recognise, which on a
    # zypper or an unlabelled entry meant a removal that could not
    # work. Failing says so; guessing a package manager does not.
    return system.CommandResult(
        args=["unsupported"],
        returncode=1,
        error=f"unsupported package type: {app['type']}",
    )


def _prune_empty_npm_scope_dir(package_id: str) -> None:
    """Remove the @scope directory an npm uninstall leaves behind empty.

    `npm uninstall -g @cloudbase/cli` takes the package and leaves
    node_modules/@cloudbase, which nothing else will ever clean. Only removed
    when it is genuinely empty, so a second package under the same scope
    keeps it.
    """
    if "/" not in package_id:
        return
    scope = package_id.split("/")[0]
    npm_root = system.run_command(["npm", "root", "-g"], capture=True, timeout=5)
    if not (npm_root.ok and npm_root.stdout.strip()):
        return
    scope_dir = Path(npm_root.stdout.strip()) / scope
    if not scope_dir.is_dir():
        return
    with contextlib.suppress(OSError):
        if not any(scope_dir.iterdir()):
            scope_dir.rmdir()


def _is_sandbox_app_data(path: Path) -> bool:
    """Whether this path is the single directory a sandboxed app keeps everything in.

    `~/.var/app/<app-id>` (Flatpak) and `~/snap/<name>` (Snap) are not a cache
    and not one config file: they are the whole of the app's user data in one
    directory -- a browser's bookmarks and saved passwords included, since a
    sandboxed browser has nowhere else to put them. So they go to the trash even
    when config.json asked for a permanent wipe: `use_trash=false` is a request
    to actually free the space a cache occupies, not a waiver on data that
    cannot be regenerated. Only the directory named after the app qualifies;
    anything deeper is already inside it and travels with it.

    Deciding this by directory root rather than by application means there is no
    per-app list to keep: whichever app is being removed, this is where a
    sandbox puts its data.
    """
    home = Path.home()
    return path.parent in (home / ".var/app", home / "snap")


def _remove_residue_paths(paths: list[Path]) -> list[tuple[bool, str]]:
    """Delete an app's leftover data, reporting what happened to each path.

    Residue removal is recoverable (trash) rather than a permanent wipe:
    residue discovery is heuristic, so a mis-matched user directory must be
    undoable. config.json's use_trash=false is the one way to ask for an
    unrecoverable wipe instead. allow_app_data_removal still lets app-owned
    data go, while hard-protected paths (whitelist, credentials, system, XDG
    user-data dirs) stay blocked.

    Only ever called once the package itself is gone -- see the caller for
    why a failed removal leaves the data where it is.
    """
    removed_details: list[tuple[bool, str]] = []
    removed_systemd_service = False
    use_trash = get_use_trash()
    for residue_path in paths:
        success, _ = safe_remove(
            residue_path,
            use_trash=use_trash or _is_sandbox_app_data(residue_path),
            allow_app_data_removal=True,
        )
        path_text = str(residue_path)
        if success and path_text.endswith(".service") and ".config/systemd/user" in path_text:
            removed_systemd_service = True
        try:
            removed_details.append((success, str(residue_path.relative_to(Path.home()))))
        except ValueError:
            removed_details.append((success, path_text))

    if removed_systemd_service and shutil.which("systemctl"):
        system.run_command(["systemctl", "--user", "daemon-reload"], capture=True, timeout=10)

    return removed_details


def execute_uninstall(app: AppRecord, paths: list[Path]):
    """Close one app's processes, remove its package, then remove its residue.

    In that order, and the last step only if the one before it succeeded. Each
    step is a call whose own name says what it does; what this function owns is
    the bookkeeping around them -- one audit event for the package removal, one
    history session that only reaches "ended" on the successful return.
    """
    app_name = str(app.get("name") or app.get("id") or "unknown")
    session_command = f"uninstall {app_name}"
    record_history_session(session_command, "started")
    package_status = "failed"
    package_event_recorded = False
    package_mode = str(app.get("type", "package")).lower()
    package_size = int(app.get("size_bytes") or 0)
    # Only the successful return below promotes this to "ended". Ctrl-C,
    # SIGTERM (which arrives as SystemExit) and a bug in the removal code all
    # leave it as it is, because `topo history` distinguishes an app whose
    # removal was cut short from one that finished with failures.
    session_status = "interrupted"

    try:
        processes.terminate_app_processes(app, paths)

        removal = _remove_package(app)
        package_status = "removed" if removal.ok else "failed"
        record_deletion_audit(app["id"], package_mode, package_status, package_size)
        package_event_recorded = True

        # Nothing is deleted while the app is still installed. The removal
        # above fails for reasons that have nothing to do with the data --
        # no polkit agent for a system-wide Flatpak, a lock held by another
        # package manager, a package the type dispatch does not know -- and
        # deleting the configuration of an app that is still there is the
        # worst of both outcomes: the user has an installed app that has
        # forgotten everything, and a retry cannot bring it back. Leaving
        # the paths alone makes the failure retryable.
        data_left_in_place = bool(paths) and package_status != "removed"
        removed_details: list[tuple[bool, str]] = []
        if package_status == "removed":
            removed_details = _remove_residue_paths(paths)

        session_status = "ended"
        return {
            "package_removed": package_status == "removed",
            "removed_paths": removed_details,
            "data_left_in_place": data_left_in_place,
        }
    finally:
        if package_status == "failed" and not package_event_recorded:
            record_deletion_audit(app.get("id", app_name), package_mode, "failed", package_size)
        record_history_session(session_command, session_status)
