"""The uninstall screen: pick applications, preview the damage, then remove them.

The scanning, residue discovery and removal all belong to UninstallManager in
the top-level uninstall module; this module only sequences them around the
selectors and reports what happened.
"""

import sys

from ...core import system
from ...core.constants import (
    BLUE,
    BOLD,
    CLEAR_LINE,
    CLEAR_SCREEN,
    CYAN,
    FAIL,
    GRAY,
    GREEN,
    INFO,
    MAGENTA,
    MARK_PROMPT,
    OK,
    PURPLE,
    RED,
    RESET,
    SUMMARY_RULE_WIDTH,
    THEME_TITLE,
    WHITE,
    AppType,
)
from ...core.file_ops import bytes_to_human
from ...core.scan_cache import ScanCache
from ...core.sound import play_delete
from ...core.spinner import threaded_spinner
from ...core.text import plural, sanitize_for_display
from ...uninstall import UninstallManager
from ..navigator import (
    Navigator,
    UninstallPreviewSelector,
    UninstallSelector,
)

# The scan spinner and the list both paint it, so it lives in one place: a
# mismatch would make the title jump the moment the scan hands off to the list.
# It names the screen rather than instructing ("Analyze Disk", not "Select a
# location..."), which is what leaves the sentence under it free to be the
# instruction without repeating the same verb twice.
SCREEN_TITLE = "Uninstall Apps"

# Only these ask a system package manager to remove something; NPM's global
# prefix and standalone CLI binaries under ~/.local are removed as the invoking
# user. Flatpak is not a fixed answer and so is not listed here: a user
# installation needs nothing, while a system-wide one lives under
# /var/lib/flatpak and is root's to remove -- UninstallManager decides that per
# app, and the same call builds the command, so the two cannot disagree.
NEEDS_SUDO_TYPES = frozenset(
    {AppType.APT, AppType.DNF, AppType.PACMAN, AppType.SNAP, AppType.ZYPPER}
)


def _print_removal_report(
    removed_names: list[str],
    failed_names: list[str],
    total_freed: int,
    *,
    interrupted: bool = False,
    data_kept: bool = False,
) -> None:
    """Print what the removal loop actually managed to do, per app and in total.

    This runs from the loop's ``finally``, because on Ctrl-C those two lists are
    the only record of which apps are already gone -- they used to be discarded
    along with the exception, leaving the user with one line ("Process
    interrupted by user.") and no way to tell what had been removed.
    """
    for name in removed_names:
        print(f"{OK} Removed {BOLD}{name}{RESET}")
    for name in failed_names:
        print(f"{FAIL} Failed to remove {BOLD}{name}{RESET}")

    # Final Summary — only report what actually succeeded.
    print(f"\n{'=' * SUMMARY_RULE_WIDTH}")
    print(f"{BLUE}{'Uninstall interrupted' if interrupted else 'Uninstall complete'}{RESET}")
    names_str = ", ".join(removed_names) if removed_names else "none"
    # Count and size get the same treatment as each other, the way the
    # preview prompt colours its pair -- but in green, not the preview's
    # purple: this is the report of what happened, not a question.
    msg = f"Removed {GREEN}{plural(len(removed_names), 'app')}{RESET}, freed {GREEN}"
    msg += f"{bytes_to_human(total_freed)}{RESET}: {names_str}"
    print(msg)
    if failed_names:
        # The label carries the emphasis, the glyph carries the colour; the names
        # themselves stay plain so they can be copied out of the report.
        print(f" {FAIL} {RED}Failed:{RESET} {', '.join(failed_names)}")
        if data_kept:
            # Said once, and only when there was data to keep: a failed removal
            # that also wiped the app's configuration would leave nothing to
            # retry with, so the two now stand or fall together.
            print(f" {INFO} {GRAY}Their application data was left in place.{RESET}")
    if interrupted:
        print(f" {INFO} {GRAY}Anything not listed above was left untouched.{RESET}")
    print("=" * SUMMARY_RULE_WIDTH)


def run_uninstall():
    manager = UninstallManager()
    # The row the list reopens on, carried by app id across a turn of the loop.
    focus_id = None

    while True:
        if not manager.has_fresh_scan_cache():

            def render_scan_spinner(frame: str) -> None:
                sys.stdout.write(
                    CLEAR_SCREEN + f"\n {THEME_TITLE}{SCREEN_TITLE}{RESET}\n\n"
                    f" {PURPLE}{frame}{RESET} {GRAY}Scanning installed applications...{RESET}\033[K"
                )
                sys.stdout.flush()

            with threaded_spinner(render_scan_spinner):
                apps = manager.run_full_scan(use_cache=True)
        else:
            apps = manager.run_full_scan(use_cache=True)

        if not apps:
            print(f"\n   {RED}No applications found to uninstall.{RESET}")
            Navigator.wait_for_return()
            return

        # A turn of this loop is one question, asked from an empty list: nothing
        # a previous turn ticked is carried into it, so the rows drawn here are
        # exactly the apps this scan found. The cursor is the one thing handed
        # back, so a cancelled uninstall reopens on the app it was about instead
        # of at the top of the list.
        selector = UninstallSelector(SCREEN_TITLE, apps, focus_id=focus_id)
        selected_indices = selector.run()

        if not selected_indices:
            return

        # Read before the preview opens, while the cursor is still where Enter
        # left it: cancelling an uninstall is usually the start of a second look
        # at the same app, and hunting for it again is the annoying part.
        focus_id = selector.focused_id

        all_targets = manager.build_removal_targets([apps[i] for i in selected_indices])

        if not UninstallPreviewSelector(all_targets).run():
            # Declining the preview backs out of the preview, not the screen, so
            # this lands on the list again -- freshly built, with the cancelled
            # ticks dropped, but with the cursor still on the app they were for.
            continue

        needs_sudo = any(
            app["type"] in NEEDS_SUDO_TYPES or UninstallManager.flatpak_removal_needs_sudo(app)
            for app, _, _ in all_targets
        )
        # Ensure sudo session (require password) outside raw mode so sudo can own input.
        if needs_sudo and not system.ensure_sudo_session(
            f"{MAGENTA}{MARK_PROMPT}{RESET} App removal requires admin access\n"
            f"{MAGENTA}{MARK_PROMPT}{RESET} Password: "
        ):
            if system.SUDO_CANCELLED:
                # Navigator.wait_for_return already adds a leading newline
                system.print_action_cancelled("Uninstall", newline=False)
                if not Navigator.wait_for_return(
                    f"Press {GREEN}Enter{RESET} {WHITE}to return to application list{RESET}, {CYAN}ESC{RESET} {WHITE}to exit...{RESET}"
                ):
                    return
                continue
            else:
                print(f" {FAIL} Authorization failed. Uninstall cancelled.\n")
                return

        if needs_sudo:
            # No trailing blank line: the removal spinner below owns the next
            # line and repaints it in place.
            system.print_sudo_granted(trailing_blank=False)

        # --- EXECUTION ---
        current_status = ["Processing..."]

        def render_removal_spinner(frame: str, status_box: list[str] = current_status) -> None:
            sys.stdout.write(f"{CLEAR_LINE}{PURPLE}{frame}{RESET} {status_box[0]}")
            sys.stdout.flush()

        removed_names: list[str] = []
        failed_names: list[str] = []
        total_freed_all = 0
        # Set by the first app whose removal failed with residue still on disk,
        # so the report can say the data is still there without naming it twice.
        data_kept = False
        # Flipped once the loop has been through every app, so any other way
        # out reports as a stop: Ctrl-C, SIGTERM (SystemExit, not
        # KeyboardInterrupt) and a bug in the removal code all leave it False.
        finished = False

        try:
            with threaded_spinner(render_removal_spinner):
                # Close everything first: the wait for SIGTERM to take effect
                # is paid once for the whole selection here, where doing it
                # inside the loop below charged it to every app in turn.
                if any(is_running for _, _, is_running in all_targets):
                    current_status[0] = "Closing running applications..."
                    manager.terminate_apps(all_targets)

                for app, paths, _ in all_targets:
                    # `name` comes from a .desktop Name= field, so it is untrusted; these
                    # lists feed the summary lines only, never a filesystem operation.
                    safe_app_name = sanitize_for_display(str(app["name"]))
                    current_status[0] = f"Removing {BOLD}{safe_app_name}{RESET}..."
                    result = manager.execute_uninstall(app, paths)
                    package_removed = bool(result.get("package_removed"))
                    paths_removed = any(ok for ok, _ in result.get("removed_paths", []))
                    if package_removed or paths_removed:
                        removed_names.append(safe_app_name)
                        if package_removed:
                            total_freed_all += app["size_bytes"]
                    else:
                        failed_names.append(safe_app_name)
                        if result.get("data_left_in_place"):
                            data_kept = True
                # No cleanup follows the loop. The system-wide `apt-get
                # autoremove --purge -y` that used to run here took every
                # unused auto-installed package on the box, previewed or not;
                # each app's own orphans now go in its own transaction, the one
                # the preview simulated (execute_uninstall passes --autoremove).
            finished = True
        finally:
            sys.stdout.write(f"{CLEAR_LINE}")
            sys.stdout.flush()
            if removed_names:
                ScanCache.clear()
                UninstallManager.clear_scan_cache()
            _print_removal_report(
                removed_names,
                failed_names,
                total_freed_all,
                interrupted=not finished,
                data_kept=data_kept,
            )

        play_delete()

        # Standardized return/exit prompt
        if not Navigator.wait_for_return(
            f"Press {GREEN}Enter{RESET} {WHITE}to return to application list{RESET}, {CYAN}ESC{RESET} {WHITE}to exit...{RESET}"
        ):
            return  # Exit uninstall completely
