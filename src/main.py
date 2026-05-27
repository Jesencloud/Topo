import os
import argparse
import sys
import time
import subprocess
from pathlib import Path

from .clean.runner import run_clean
from .clean.project import run_purge
from .clean.app_manager import run_uninstall
from .clean.optimize import optimize_system
from .core.system import get_os_id, ensure_sudo_session, setup_passwordless_sudo
from .core.status import show_status
from .core.analyze import run_deep_analysis
from .core.whitelist import add_to_whitelist, remove_from_whitelist
from .core.constants import BLUE, CYAN, MAGENTA, YELLOW, GREEN, RED, WHITE, GRAY, RESET, BOLD
from .manage.remove import run_remove
from .manage.install import run_install_link
from .manage.update import run_update
from .ui.tui import main_menu

# Get version from root VERSION file
VERSION_FILE = Path(__file__).parent.parent / "VERSION"
TOPO_VERSION = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "0.5.0"

def main():
    parser = argparse.ArgumentParser(
        description="topo - High-performance Linux System Optimizer (Inspired by Mole)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  topo                 Start interactive TUI (Recommended)
  topo clean           Run one-key safe cleanup
  topo analyze         Start interactive disk usage explorer
  topo status          Check system health and metrics
  topo update          Upgrade to the latest version
  topo whitelist list  View currently protected paths
  topo --dry-run clean Preview files to be cleaned without deleting
"""
    )
    
    # Use a subparser for better help organization
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- Core Actions ---
    subparsers.add_parser("clean", help="One-key safe disk cleanup")
    subparsers.add_parser("update", help="Update topo to the latest version")
    subparsers.add_parser("purge", help="Interactive project artifact purging")
    subparsers.add_parser("uninstall", help="Completely remove applications and residues")
    subparsers.add_parser("optimize", help="Run system maintenance (fstrim, databases, etc.)")
    subparsers.add_parser("analyze", help="Interactive disk usage explorer")
    subparsers.add_parser("status", help="Monitor system health and resource usage")
    
    # --- Management ---
    link_parser = subparsers.add_parser("link", help="Create a symbolic link in ~/.local/bin for 'topo' command")
    link_parser.add_argument("--silent", action="store_true", help="Suppress success banner")
    
    wl_parser = subparsers.add_parser("whitelist", help="Manage path protection whitelist")
    wl_parser.add_argument("action", choices=["add", "remove", "list"], nargs="?", default="list", help="Whitelist action")
    wl_parser.add_argument("path", nargs="?", help="Target path for add/remove")
    
    subparsers.add_parser("authorize", help="Setup passwordless sudo for faster cleanup")
    subparsers.add_parser("remove", help="Uninstall topo from the system")
    subparsers.add_parser("all", help="Run all cleanup and purge tasks sequentially")

    # --- Global Options ---
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without deleting")
    parser.add_argument("--version", action="version", version=f"topo {TOPO_VERSION}")
    
    args = parser.parse_args()

    # Authorization setup command
    if args.command == "authorize":
        setup_passwordless_sudo()
        return

    # Whitelist Management CLI
    if args.command == "whitelist":
        if args.action == "add" and args.path:
            if add_to_whitelist(args.path):
                print(f"✅ Added to whitelist: {args.path}")
            else:
                print(f"ℹ️  Path already whitelisted: {args.path}")
        elif args.action == "remove" and args.path:
            if remove_from_whitelist(args.path):
                print(f"✅ Removed from whitelist: {args.path}")
            else:
                print(f"❌ Path not found in whitelist: {args.path}")
        elif args.action == "list" or not args.action:
            from .core.whitelist import get_whitelist
            w = get_whitelist()
            print("\033[1;36m🛡️  Current Whitelist:\033[0m")
            if not w:
                print("   (Empty)")
            for p in w:
                print(f"   - {p}")
        else:
            print("Usage: topo whitelist <add|remove|list> [path]")
        return

    # If no command is provided, enter TUI
    if args.command is None:
        while True:
            choice = main_menu()
            if choice == "1":
                run_clean(args.dry_run)
                input("\n\033[1;90mPress Enter to return to Main Menu...\033[0m")
            elif choice == "2":
                run_uninstall()
            elif choice == "3":
                print("\033[1;90m🔒 Authorizing optimization tasks (Ctrl+C to cancel)...\033[0m")
                if ensure_sudo_session():
                    optimize_system(args.dry_run)
                else:
                    print("\033[1;33m⚠️  Optimization cancelled by user.\033[0m")
                input("\nPress Enter to return to menu...")
            elif choice == "4":
                run_deep_analysis()
            elif choice == "5":
                show_status()
                input("\nPress Enter to return to menu...")
            elif choice == "0" or choice.lower() == "q":
                print("Goodbye!")
                break
        return

    # CLI Mode Execution
    print(f"\033[1;34mtopo {TOPO_VERSION} (Python Edition)\033[0m")
    os_id = get_os_id()
    print(f"System: {os_id}\n")

    if args.command in ("clean", "all"):
        run_clean(args.dry_run)

    if args.command in ("purge", "all"):
        run_purge(args.dry_run)

    if args.command == "uninstall":
        run_uninstall()

    if args.command == "analyze":
        run_deep_analysis()

    if args.command == "status":
        show_status()

    if args.command == "optimize":
        print("\033[1;90m🔒 Authorizing optimization tasks (Ctrl+C to cancel)...\033[0m")
        if ensure_sudo_session():
            optimize_system(args.dry_run)
        else:
            print("\033[1;33m⚠️  Optimization cancelled by user.\033[0m")

    if args.command == "link":
        run_install_link(silent=args.silent)

    if args.command == "update":
        run_update()

    if args.command == "remove":
        run_remove(args.dry_run)

if __name__ == "__main__":
    main()
