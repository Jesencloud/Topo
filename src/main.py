import argparse
import sys
from contextlib import contextmanager

from .clean.runner import run_clean
from .core import system, terminal_state
from .core.constants import (
    BLUE,
    CLEAR_LINE,
    CLEAR_SCREEN,
    RESET,
    THEME_TITLE,
    TOPO_VERSION,
    setup_color_mode,
)
from .core.history import show_history
from .core.lock import SingleInstanceLock
from .core.whitelist import add_to_whitelist, remove_from_whitelist
from .manage.doctor import run_doctor
from .manage.install import run_install_link
from .manage.remove import run_remove
from .manage.update import run_update
from .optimize import optimize_system
from .status import show_status
from .ui.navigator import Navigator
from .ui.screens.analyze import run_deep_analysis
from .ui.screens.uninstall import run_uninstall
from .ui.tui import (
    ANALYZE_ACTION,
    CLEAN_ACTION,
    OPTIMIZE_ACTION,
    QUIT_ACTION,
    STATUS_ACTION,
    UNINSTALL_ACTION,
    main_menu,
)

DRY_RUN_HELP = "Preview changes without deleting"
INTERRUPTED_MESSAGE = "🚫 Process interrupted by user."

MAIN_HELP = """
Quick Start:
  topo                     Open the interactive TUI
  topo clean --dry-run     Preview cleanup without deleting
  topo analyze             Explore disk usage
  topo status              Show system health
  topo doctor              Diagnose Topo installation and runtime tools
  topo history --limit 5   Show the last 5 cleanup sessions

Whitelist:
  topo whitelist list         Show manual protection rules.
  topo whitelist add PATH     Protect PATH from cleanup.
  topo whitelist remove PATH  Remove a manual rule.

Notes:
  An empty whitelist is normal before you add a path.
  Built-in protections cover system paths, credentials, and XDG folders.
  Run topo whitelist --help for whitelist details.
  Run topo COMMAND --help for command-specific options.
"""

WHITELIST_HELP = """
Examples:
  topo whitelist list               Show manual protection rules.
  topo whitelist add ~/Projects     Protect ~/Projects and its children.
  topo whitelist remove ~/Projects  Remove a manual rule.

Notes:
  Manual rules are stored in ~/.config/topo/whitelist.json.
  Built-in protections are not shown by whitelist list.
"""

CLEAN_HELP = """
Examples:
  topo clean             Run safe disk cleanup
  topo clean --dry-run   Preview cleanup without deleting
"""

OPTIMIZE_HELP = """
Examples:
  topo optimize             Run system maintenance
  topo optimize --dry-run   Preview maintenance changes
"""

REMOVE_HELP = """
Examples:
  topo remove             Uninstall topo from the system
  topo remove --dry-run   Preview files and links that would be removed
"""

HISTORY_HELP = """
Examples:
  topo history
  topo history --limit 5
"""


@contextmanager
def alternate_screen():
    """Context manager to use the terminal's alternate screen buffer."""
    terminal_state.enter_alternate_screen()
    try:
        yield
    finally:
        terminal_state.exit_alternate_screen()


@contextmanager
def main_screen():
    """Temporarily drop to the main screen buffer from within an alternate-screen span.

    The TUI menu loop holds one continuous alternate-screen session (so selecting an
    alternate-screen action never flashes the shell prompt between the menu and the
    action). Report-style commands (clean/optimize/status) still want to print into
    the main buffer so their output survives in the scrollback after topo exits; this
    steps back to the main buffer for the command's duration and restores the
    alternate screen on the way out.
    """
    terminal_state.exit_alternate_screen()
    try:
        yield
    finally:
        terminal_state.enter_alternate_screen()


def _clear_screen():
    sys.stdout.write(CLEAR_SCREEN)
    sys.stdout.flush()


def _print_interrupted(clear_screen=False):
    terminal_state.reset_terminal(force=True)
    if clear_screen:
        _clear_screen()
    else:
        sys.stdout.write(CLEAR_LINE)
        sys.stdout.flush()
    print(INTERRUPTED_MESSAGE)


def _run_terminal_tui_command(command, *args):
    with main_screen():
        try:
            _clear_screen()
            result = command(*args)
            if result is False:
                return True
            return Navigator.wait_for_return()
        except KeyboardInterrupt:
            _print_interrupted(clear_screen=True)
            return False


def _run_alternate_tui(command, *args):
    with alternate_screen():
        _clear_screen()
        return command(*args)


def main():
    terminal_state.install_signal_handlers()
    try:
        _main()
    except KeyboardInterrupt:
        _print_interrupted(clear_screen=True)
        raise SystemExit(130) from None


def _main():
    parser = argparse.ArgumentParser(
        prog="topo",
        description="topo - Linux cleanup, app removal, disk analysis, and status checks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=MAIN_HELP,
    )

    dry_run_parent = argparse.ArgumentParser(add_help=False)
    dry_run_parent.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help=DRY_RUN_HELP,
    )

    # Use a subparser for better help organization
    subparsers = parser.add_subparsers(title="commands", dest="command", metavar="COMMAND")

    # --- Core Actions ---
    subparsers.add_parser(
        "clean",
        parents=[dry_run_parent],
        help="One-key safe disk cleanup",
        description="Run safe disk cleanup.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=CLEAN_HELP,
    )
    subparsers.add_parser("analyze", help="Interactive disk usage explorer")
    subparsers.add_parser("uninstall", help="Completely remove applications and residues")
    subparsers.add_parser(
        "optimize",
        parents=[dry_run_parent],
        help="Run system maintenance (fstrim, databases, etc.)",
        description="Run system maintenance tasks.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=OPTIMIZE_HELP,
    )
    subparsers.add_parser("status", help="Monitor system health and resource usage")
    subparsers.add_parser(
        "doctor", help="Run a comprehensive diagnostic check of the Topo environment"
    )
    history_parser = subparsers.add_parser(
        "history",
        help="Show recent deletion history",
        description="Show recent cleanup sessions.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=HISTORY_HELP,
    )
    history_parser.add_argument(
        "--limit",
        type=int,
        default=10,
        metavar="N",
        help="Number of sessions to show (default: 10)",
    )

    # --- Management ---
    subparsers.add_parser("authorize", help="Setup passwordless sudo for faster cleanup")
    subparsers.add_parser("update", help="Update topo to the latest version")
    subparsers.add_parser(
        "remove",
        parents=[dry_run_parent],
        help="Uninstall topo from the system",
        description="Uninstall topo from the system.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=REMOVE_HELP,
    )
    link_parser = subparsers.add_parser(
        "link", help="Create a symbolic link for the 'topo' command"
    )
    link_parser.add_argument("--silent", action="store_true", help="Suppress success banner")

    wl_parser = subparsers.add_parser(
        "whitelist",
        help="Manage manual path protection whitelist",
        description="Manage the manual path protection whitelist.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=WHITELIST_HELP,
    )
    wl_parser.add_argument(
        "action",
        choices=["add", "remove", "list"],
        nargs="?",
        default="list",
        metavar="ACTION",
        help="Action to run: add, remove, or list (default: list)",
    )
    wl_parser.add_argument("path", nargs="?", metavar="PATH", help="Target path for add/remove")

    # --- Global Options ---
    parser.add_argument("--version", action="version", version=f"topo {TOPO_VERSION}")
    # The wording is deliberately narrow: setup_color_mode() blanks every color
    # that flows through core.constants, which covers all report/status output.
    # The interactive selectors still carry inline SGR literals for cursors and
    # hover rows; those are not blanked, but every one of them is redundant with
    # a glyph (▶ / ✓ / > / ▸ Yes ◂), so a colorless terminal loses decoration and
    # not information. Keep it that way: if you add a state that only color
    # distinguishes, give it a glyph too (tests/test_navigator.py enforces this
    # for the confirmation dialog).
    parser.add_argument(
        "--no-color",
        action="store_true",
        help=(
            "Disable colored output in reports and status views "
            "(respects NO_COLOR, https://no-color.org/); "
            "interactive menus keep some decorative highlighting"
        ),
    )

    args = parser.parse_args()
    setup_color_mode(getattr(args, "no_color", False))
    dry_run = getattr(args, "dry_run", False)

    # Authorization setup command
    if args.command == "authorize":
        system.setup_passwordless_sudo()
        return

    # Whitelist Management CLI
    if args.command == "whitelist":
        if args.action in ("add", "remove") and not args.path:
            wl_parser.error(f"{args.action} requires PATH")
        if args.action == "list" and args.path:
            wl_parser.error("list does not accept PATH")

        if args.action == "add":
            if add_to_whitelist(args.path):
                print(f"✅ Added to whitelist: {args.path}")
            else:
                print(f"ℹ️  Path already whitelisted: {args.path}")
        elif args.action == "remove":
            if remove_from_whitelist(args.path):
                print(f"✅ Removed from whitelist: {args.path}")
            else:
                print(f"❌ Path not found in whitelist: {args.path}")
                sys.exit(1)
        elif args.action == "list":
            from .core.whitelist import get_whitelist

            w = get_whitelist()
            print(f"{THEME_TITLE}🛡️  Current Whitelist:{RESET}")
            if not w:
                print("   (Empty)")
            for p in w:
                print(f"   - {p}")
        return

    # Commands requiring single-instance concurrency lock.
    #
    # update and remove belong here even though they delete nothing under the
    # user's home: their blast radius is the installation itself. update hands
    # ~/.topo to install.sh, which replaces the whole directory, and remove
    # deletes it with allow_self_removal=True -- both while a concurrent
    # `topo clean` may still be importing modules out of it. That makes them
    # more destructive than the five commands the lock was originally written
    # for, not less.
    LOCK_REQUIRED_COMMANDS = {
        "clean",
        "uninstall",
        "optimize",
        "analyze",
        "update",
        "remove",
        None,
    }

    if args.command in LOCK_REQUIRED_COMMANDS:
        with SingleInstanceLock():
            _execute_main_router(args, dry_run)
    else:
        _execute_main_router(args, dry_run)


def _execute_main_router(args, dry_run):
    # If no command is provided, enter TUI
    if args.command is None:
        menu_routes = {
            CLEAN_ACTION: lambda: _run_terminal_tui_command(run_clean, dry_run),
            UNINSTALL_ACTION: lambda: _run_alternate_tui(run_uninstall) or True,
            OPTIMIZE_ACTION: lambda: _run_terminal_tui_command(optimize_system, dry_run),
            ANALYZE_ACTION: lambda: _run_alternate_tui(run_deep_analysis) or True,
            STATUS_ACTION: lambda: _run_terminal_tui_command(show_status),
        }
        menu_indices = {
            CLEAN_ACTION: 0,
            UNINSTALL_ACTION: 1,
            OPTIMIZE_ACTION: 2,
            ANALYZE_ACTION: 3,
            STATUS_ACTION: 4,
        }
        selected_menu_index = 0
        with alternate_screen():
            terminal_state.hide_cursor()
            try:
                while True:
                    choice = main_menu(selected_menu_index)

                    if choice == QUIT_ACTION:
                        break
                    if choice in menu_indices:
                        selected_menu_index = menu_indices[choice]
                    action = menu_routes.get(choice)
                    if action and not action():
                        break
            finally:
                terminal_state.show_cursor()
        return

    # CLI Mode Execution
    # Suppress version banner for silent link command to keep installation log clean
    if args.command not in ("analyze", "uninstall") and not (
        args.command == "link" and args.silent
    ):
        print(f"{BLUE}topo {TOPO_VERSION} (Python Edition){RESET}")
        os_id = system.get_os_id()
        print(f"System: {os_id}")

    if args.command == "clean":
        run_clean(dry_run)

    if args.command == "uninstall":
        with alternate_screen():
            run_uninstall()

    if args.command == "analyze":
        with alternate_screen():
            run_deep_analysis()

    if args.command == "status":
        show_status()

    if args.command == "doctor":
        run_doctor()

    if args.command == "history":
        show_history(limit=max(args.limit, 1))

    if args.command == "optimize":
        optimize_system(dry_run)

    if args.command == "link" and not run_install_link(silent=args.silent):
        sys.exit(1)

    if args.command == "update":
        run_update()

    if args.command == "remove":
        run_remove(dry_run)


if __name__ == "__main__":
    main()
