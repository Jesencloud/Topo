"""What else comes off the machine when one package is removed.

`apt-get purge`, `dnf remove`, `pacman -Rns` and `zypper remove` all pull out
whatever depends on the package being removed, so ticking one small entry can
drag out half a desktop -- and the preview used to list only the entry itself and
the residue paths beside it. This module answers that one question, for the
confirmation screen, and does nothing else.

Every query here is read-only and runs as the invoking user. That is not an
implementation detail: the preview is drawn before the password is asked for, so
a query that wanted root would undo the point of only asking when a removal
needs it. The rpm side has no unprivileged exact dry-run at all, which is why
those two are asked "what requires this" instead -- see
:func:`collateral_packages`.

Split out of ``UninstallManager`` because it shares no state with the class and
touches nothing on disk: this is the one part of the removal path that only
asks questions. ``build_removal_targets`` stays in ``manager.py`` and calls
:func:`collateral_packages` once per selected app across a thread pool.
"""

from ..core import system
from ..core.constants import AppType
from ..core.package_manager import DNF, resolve_admin_tool
from .discovery import AppRecord, strip_package_arch


def collateral_packages(app: AppRecord) -> list[str]:
    """Which other installed packages this app's removal will take with it.

    Every query here is read-only and runs as the invoking user, because the
    preview is drawn before the password is asked for, and asking for one
    earlier just to draw it would undo the point of only asking when a removal
    needs root. That rules out the exact dry-runs on the rpm side (`dnf remove
    --assumeno`, `rpm -e --test` and `zypper remove --dry-run` all want the
    database lock), so those two are asked what requires the package instead:
    apt and pacman answer with the whole transitive set they would really
    remove, the rpm family with its first level. The list is therefore a floor
    rather than a promise, and a failed or unparsable reply yields an empty
    one -- the preview then says nothing, exactly as it did before.

    The apt query carries the same flags as the apt removal in
    execute_uninstall, `-s` in place of `-y`, so on Debian the floor is the
    transaction: what is listed here is what that removal takes.
    """
    app_id = str(app.get("id") or "")
    app_type = str(app.get("type") or "")
    if not app_id:
        return []
    if app_type == AppType.APT:
        # -s simulates without root; the whole transaction is narrated, and
        # the removal lines are the interesting ones.
        #
        # --autoremove is what puts the dependencies this removal orphans into
        # that transaction. Without it apt names them only in its "packages
        # were automatically installed and are no longer required" prose, with
        # no Remv/Purg prefix, so the preview omitted precisely the packages
        # the removal went on to take. Measured on debian:stable-slim:
        # `purge -s cowsay` narrates one Purg line, `purge --autoremove -s
        # cowsay` narrates eight.
        argv = ["apt-get", "purge", "--autoremove", "-s", app_id]
        env = system.APT_NONINTERACTIVE_ENV
    elif app_type == AppType.PACMAN:
        # --print-format implies --print, and --print is what makes pacman
        # skip the database lock it would otherwise need root for. %n asks
        # for bare names, so nothing here goes through a message catalog.
        argv = ["pacman", "-Rns", "--print-format", "%n", app_id]
        env = system.C_LOCALE_ENV
    elif app_type == AppType.DNF:
        dnf_cmd = resolve_admin_tool(DNF)
        # -C keeps it off the network: the installed set is all we ask about.
        argv = [
            dnf_cmd,
            "repoquery",
            "-C",
            "--installed",
            "--whatrequires",
            app_id,
            "--qf",
            "%{name}\n",
        ]
        env = system.C_LOCALE_ENV
    elif app_type == AppType.ZYPPER:
        # zypper has no unprivileged dry-run, and rpm is on every zypper box.
        argv = ["rpm", "-q", "--whatrequires", app_id, "--qf", "%{NAME}\n"]
        env = system.C_LOCALE_ENV
    else:
        # A Flatpak, Snap, NPM or CLI removal takes nothing else with it.
        return []

    # run_command turns a missing binary or a timeout into a CommandResult
    # rather than raising, and the parser below keeps only bare package names,
    # so a failed or half-finished reply comes out as an empty list. rpm exits
    # 1 with "no package requires X" on stdout when there are none, which is
    # why the return code is not consulted.
    simulated = system.run_command(argv, capture=True, timeout=30, env=env)
    return _parse_collateral(simulated.stdout, app_id, app_type)


def _parse_collateral(stdout: str, app_id: str, app_type: str) -> list[str]:
    """Package names out of a simulated removal or a reverse-dependency reply."""
    names: list[str] = []
    for line in stdout.splitlines():
        entry = line.strip()
        if app_type == AppType.APT:
            # "Remv firefox [1:2snap1-0ubuntu2]", among Inst/Conf lines and
            # apt's own prose.
            fields = entry.split()
            if len(fields) < 2 or fields[0] not in ("Remv", "Purg"):
                continue
            # A foreign-arch package is narrated qualified (libfoo:i386) while
            # the scan stripped the qualifier off its id, so without this the
            # app fails to recognise itself in its own transaction.
            entry = strip_package_arch(fields[1])
        # rpm reports "no package requires X" on stdout, and a package name
        # never holds whitespace, so this drops prose without matching on it.
        if not entry or len(entry.split()) != 1 or entry == app_id:
            continue
        if entry not in names:
            names.append(entry)
    return names
