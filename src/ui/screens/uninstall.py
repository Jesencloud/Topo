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
    GRAY,
    GREEN,
    MAGENTA,
    PURPLE,
    RED,
    RESET,
    THEME_TITLE,
    WHITE,
    YELLOW,
)
from ...core.file_ops import bytes_to_human
from ...core.scan_cache import ScanCache
from ...core.sound import play_delete
from ...core.spinner import threaded_spinner
from ...core.text import sanitize_for_display
from ...uninstall import UninstallManager
from ..navigator import Navigator, UninstallPreviewSelector, UninstallSelector

# The scan spinner and the list both paint it, so it lives in one place: a
# mismatch would make the title jump the moment the scan hands off to the list.
# It names the screen rather than instructing ("Analyze Disk", not "Select a
# location..."), which is what leaves the sentence under it free to be the
# instruction without repeating the same verb twice.
SCREEN_TITLE = "Uninstall Apps"


def run_uninstall():
    manager = UninstallManager()

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

        selector = UninstallSelector(SCREEN_TITLE, apps)
        selected_indices = selector.run()

        if not selected_indices:
            return

        all_targets = manager.build_removal_targets([apps[i] for i in selected_indices])

        confirmed = UninstallPreviewSelector(all_targets).run()

        if confirmed:
            # Ensure sudo session (require password) outside raw mode so sudo can own input.
            if not system.ensure_sudo_session(
                f"{MAGENTA}➔{RESET} App removal requires admin access\n{MAGENTA}➔{RESET} Password: "
            ):
                if system.SUDO_CANCELLED:
                    # Navigator.wait_for_return already adds a leading newline
                    print(f" {YELLOW}⚠️  Uninstall cancelled by user.{RESET}", end="")
                    if not Navigator.wait_for_return(
                        f"Press {GREEN}Enter{RESET} {WHITE}to return to application list{RESET}, {CYAN}ESC{RESET} {WHITE}to exit...{RESET}"
                    ):
                        return
                    continue
                else:
                    print(f" {RED}✗{RESET} Authorization failed. Uninstall cancelled.\n")
                    return

            print(f"{GREEN}ꗃ{RESET} Authorization successful.")

            # --- EXECUTION ---
            current_status = ["Processing..."]

            def render_removal_spinner(frame: str, status_box: list[str] = current_status) -> None:
                sys.stdout.write(f"{CLEAR_LINE}{PURPLE}{frame}{RESET} {status_box[0]}")
                sys.stdout.flush()

            removed_names = []
            failed_names = []
            total_freed_all = 0
            has_apt = False

            try:
                with threaded_spinner(render_removal_spinner):
                    for app, paths, _ in all_targets:
                        # `name` comes from a .desktop Name= field, so it is untrusted; these
                        # lists feed the summary lines only, never a filesystem operation.
                        safe_app_name = sanitize_for_display(str(app["name"]))
                        current_status[0] = f"Removing {BOLD}{safe_app_name}{RESET}..."
                        if app["type"] == "APT":
                            has_apt = True
                        result = manager.execute_uninstall(app, paths)
                        package_removed = bool(result.get("package_removed"))
                        paths_removed = any(ok for ok, _ in result.get("removed_paths", []))
                        if package_removed or paths_removed:
                            removed_names.append(safe_app_name)
                            if package_removed:
                                total_freed_all += app["size_bytes"]
                        else:
                            failed_names.append(safe_app_name)

                    if has_apt and removed_names:
                        current_status[0] = "Cleaning up orphaned dependencies..."
                        system.run_command(["apt", "autoremove", "-y"], use_sudo=True, capture=True)
            finally:
                sys.stdout.write(f"{CLEAR_LINE}")
                sys.stdout.flush()

            for name in removed_names:
                print(f"{GREEN}✓{RESET} Removed {BOLD}{name}{RESET}")
            for name in failed_names:
                print(f"{RED}✗{RESET} Failed to remove {BOLD}{name}{RESET}")

            # Final Summary — only report what actually succeeded.
            if removed_names:
                ScanCache.clear()
                UninstallManager.clear_scan_cache()
            print(f"\n{'=' * 70}")
            print(f"{BLUE}Uninstall complete{RESET}")
            names_str = ", ".join(removed_names) if removed_names else "none"
            # Count and size get the same treatment as each other, the way the
            # preview prompt colours its pair -- but in green, not the preview's
            # purple: this is the report of what happened, not a question.
            msg = f"Removed {GREEN}{len(removed_names)}{RESET} app(s), freed {GREEN}"
            msg += f"{bytes_to_human(total_freed_all)}{RESET}: {names_str}"
            print(msg)
            if failed_names:
                print(f" {RED}✗ Failed:{RESET} {', '.join(failed_names)}")
            print("=" * 70)
            play_delete()

            # Standardized return/exit prompt
            if not Navigator.wait_for_return(
                f"Press {GREEN}Enter{RESET} {WHITE}to return to application list{RESET}, {CYAN}ESC{RESET} {WHITE}to exit...{RESET}"
            ):
                return  # Exit uninstall completely
