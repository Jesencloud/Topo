import argparse
import sys
from collections.abc import Callable
from contextlib import contextmanager

from .clean.runner import run_clean
from .core import system, terminal_state
from .core.config import get_theme_color
from .core.constants import (
    BLUE,
    BOLD,
    CLEAR_LINE,
    CLEAR_SCREEN,
    GRAY,
    RED,
    RESET,
    THEME_TITLE,
    TOPO_VERSION,
    YELLOW,
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
  topo remove --yes       Uninstall without the confirmation prompt (scripts, CI)
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


# Exit-code contract (the reason every run_xxx() below returns a bool)
#
# 0  the command did what it was asked to do -- including "nothing to do"
#    (already up to date, no residue to remove, an empty whitelist)
# 1  it did not: a failure, a refusal, or a cancellation
# 2  argparse rejected the arguments (argparse's own convention)
# 130 interrupted (see main())
#
# Before this contract every command exited 0 no matter what, so a failed
# signature verification, a package manager that returned non-zero, a `remove`
# that could not delete anything and a `doctor` that found a dead engine were
# all indistinguishable from success -- `topo update && echo ok` printed ok.
# That made topo unusable in a script, which is exactly where doctor and update
# belong.
#
# The convention that keeps this cheap: an action returning None counts as
# success. Pure report commands (status, history, analyze, uninstall) have no
# failure to report and stay annotation-free; only commands with a real
# pass/fail outcome return a bool. A user cancelling a confirmation counts as
# failure, not success: the operation did not happen, and `topo remove && ...`
# must not run the rest.
def main():
    terminal_state.install_signal_handlers()
    try:
        ok = _main()
    except KeyboardInterrupt:
        _print_interrupted(clear_screen=True)
        raise SystemExit(130) from None
    if not ok:
        raise SystemExit(1)


def _main() -> bool:
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
    remove_parser = subparsers.add_parser(
        "remove",
        parents=[dry_run_parent],
        help="Uninstall topo from the system",
        description="Uninstall topo from the system.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=REMOVE_HELP,
    )
    # The confirmation prompt needs a terminal, so automation (CI smoke tests,
    # image builds, provisioning) needs a way past it -- without one the only
    # alternative is `apt remove topo`, which skips the user-residue cleanup that
    # `topo remove` exists for.
    remove_parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt (for scripts and CI)",
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
    setup_color_mode(getattr(args, "no_color", False), theme_color=get_theme_color())
    dry_run = getattr(args, "dry_run", False)

    # Authorization setup command
    #
    # authorize and whitelist stay ahead of the dispatch table on purpose: they
    # run before the version banner (both are quiet, script-friendly commands)
    # and they need neither the interactive-terminal guard nor the lock.
    if args.command == "authorize":
        return system.setup_passwordless_sudo()

    # Whitelist Management CLI
    if args.command == "whitelist":
        if args.action in ("add", "remove") and not args.path:
            wl_parser.error(f"{args.action} requires PATH")
        if args.action == "list" and args.path:
            wl_parser.error("list does not accept PATH")

        if args.action == "add":
            # A duplicate is not a failure: adding a path that is already
            # protected leaves the system in the state the caller asked for, so
            # `topo whitelist add` is safe to run unconditionally in a script.
            if add_to_whitelist(args.path):
                print(f"✅ Added to whitelist: {args.path}")
            else:
                print(f"ℹ️  Path already whitelisted: {args.path}")
        elif args.action == "remove":
            if remove_from_whitelist(args.path):
                print(f"✅ Removed from whitelist: {args.path}")
            else:
                print(f"❌ Path not found in whitelist: {args.path}")
                return False
        elif args.action == "list":
            from .core.whitelist import get_whitelist

            w = get_whitelist()
            print(f"{THEME_TITLE}🛡️  Current Whitelist:{RESET}")
            if not w:
                print("   (Empty)")
            for p in w:
                print(f"   - {p}")
        return True

    # Commands that cannot work without a terminal.
    #
    # The TUI menu, analyze and uninstall all drive Navigator.raw_mode(), whose
    # first act is termios.tcgetattr(stdin). termios.error is not an OSError
    # subclass, so anything that hands topo a non-terminal stdin -- `echo | topo`,
    # `topo analyze < /dev/null`, cron, non-interactive ssh -- used to escape
    # every handler on the way up and print a raw traceback. (`topo | cat` only
    # redirects stdout and never hit this.) Guarding here covers all three entry
    # points at once and keeps isatty knowledge out of the ui layer; raw_mode()
    # itself must not degrade to a no-op, or the selector loop would spin against
    # a pipe instead.
    if args.command in {None, "analyze", "uninstall"} and not sys.stdin.isatty():
        label = f"topo {args.command}" if args.command else "The topo menu"
        print(f"\n {YELLOW}⚠{RESET} {label} needs an interactive terminal.")
        print(
            f"  {GRAY}Run it from a terminal, or use a non-interactive command such as{RESET} "
            f"{BOLD}topo status{RESET}{GRAY} or{RESET} {BOLD}topo clean --dry-run{RESET}{GRAY}.{RESET}"
        )
        return False

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
            return _execute_main_router(args, dry_run)
    return _execute_main_router(args, dry_run)


def _run_menu_loop(dry_run):
    # Keyed in the order main_menu() draws the entries: the cursor position the
    # menu reopens on is this dict's index for the action just run, so a second
    # action -> index table cannot drift out of sync with the routes.
    menu_routes = {
        CLEAN_ACTION: lambda: _run_terminal_tui_command(run_clean, dry_run),
        UNINSTALL_ACTION: lambda: _run_alternate_tui(run_uninstall) or True,
        OPTIMIZE_ACTION: lambda: _run_terminal_tui_command(optimize_system, dry_run),
        ANALYZE_ACTION: lambda: _run_alternate_tui(run_deep_analysis) or True,
        STATUS_ACTION: lambda: _run_terminal_tui_command(show_status),
    }
    menu_order = list(menu_routes)
    selected_menu_index = 0
    with alternate_screen():
        terminal_state.hide_cursor()
        try:
            while True:
                choice = main_menu(selected_menu_index)

                if choice == QUIT_ACTION:
                    break
                action = menu_routes.get(choice)
                if action is None:
                    continue
                selected_menu_index = menu_order.index(choice)
                if not action():
                    break
        finally:
            terminal_state.show_cursor()


def _execute_main_router(args, dry_run) -> bool:
    # If no command is provided, enter TUI. The menu is a session, not an
    # operation: quitting it is success even if a cleanup inside it failed, and
    # the per-action result is what ends the loop (see _run_terminal_tui_command).
    if args.command is None:
        _run_menu_loop(dry_run)
        return True

    # CLI Mode Execution
    # Suppress version banner for silent link command to keep installation log clean
    if args.command not in ("analyze", "uninstall") and not (
        args.command == "link" and args.silent
    ):
        print(f"{BLUE}topo {TOPO_VERSION} (Python Edition){RESET}")
        os_id = system.get_os_id()
        print(f"System: {os_id}")

    # One dispatch table instead of eleven sequential `if args.command == ...`
    # comparisons: a command can no longer be silently unroutable (adding a
    # subparser without a branch used to run, print the banner and exit 0), and
    # every route funnels through the same success/failure conversion below.
    dispatch: dict[str, Callable[[], bool | None]] = {
        "clean": lambda: run_clean(dry_run),
        "uninstall": lambda: _run_alternate_tui(run_uninstall),
        "analyze": lambda: _run_alternate_tui(run_deep_analysis),
        "optimize": lambda: optimize_system(dry_run),
        "status": show_status,
        "doctor": run_doctor,
        "history": lambda: show_history(limit=max(args.limit, 1)),
        "link": lambda: run_install_link(silent=args.silent),
        "update": run_update,
        "remove": lambda: run_remove(dry_run, args.yes),
    }

    action = dispatch.get(args.command)
    if action is None:
        # Unreachable through argparse, which rejects unknown commands with
        # exit 2. It fires for a subparser added without a route -- a mistake
        # that used to be invisible.
        print(f"\n {RED}✗{RESET} Command {args.command!r} has no handler.", file=sys.stderr)
        return False

    # None means "no failure to report" (status, history, analyze, uninstall);
    # only an explicit False is a failure.
    return action() is not False


if __name__ == "__main__":
    main()
