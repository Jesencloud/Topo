import os
import argparse
import sys
import time
from pathlib import Path
from .clean.system import clean_package_manager, clean_journal
from .clean.dev import clean_developer_tools
from .clean.user import clean_user_data
from .clean.apps import clean_apps_deep
from .clean.optimize import optimize_system
from .core.system import get_os_id, ensure_sudo_session, setup_passwordless_sudo
from .core.status import show_status
from .core.analyze import run_deep_analysis
from .purge.manager import PurgeManager
from .ui.menu import interactive_select
from .ui.tui import main_menu

# ANSI Colors
BLUE = "\033[1;34m"
CYAN = "\033[1;36m"
MAGENTA = "\033[1;35m"
YELLOW = "\033[1;33m"
GREEN = "\033[1;32m"
RED = "\033[1;31m"
WHITE = "\033[1;37m"
GRAY = "\033[1;90m"
RESET = "\033[0m"
BOLD = "\033[1m"

def run_clean(dry_run=False):
    from .core.file_ops import bytes_to_human
    import shutil
    
    # 1. Prepare categories
    categories = [
        {"name": "Package Manager Cache", "func": clean_package_manager},
        {"name": "System Journal Logs", "func": clean_journal},
        {"name": "User Data & Trash", "func": clean_user_data},
        {"name": "Deep App Caches", "func": clean_apps_deep},
        {"name": "Developer Artifacts", "func": clean_developer_tools}
    ]

    mode_label = "\033[1;36m[PREVIEW]\033[0m" if dry_run else "\033[1;95m[EXECUTING]\033[0m"
    print(f"{mode_label} Starting system cleanup...\n")

    # Pre-authorize sudo to avoid interrupting the progress list
    if not dry_run:
        print(f" {GRAY}🔒 Authorizing system-level tasks...{RESET}")
        if not ensure_sudo_session():
            print(f" {YELLOW}⚠️  Note: Some system caches will be skipped due to lack of permission.{RESET}\n")
        else:
            print(f" {GREEN}✓{RESET} Authorization successful.\n")

    total_size = 0
    total_items = 0
    total_categories = 0

    # 2. Execute all categories one by one
    for cat in categories:
        # We print a clean progress line
        s, i, c = cat["func"](dry_run=dry_run)
        total_size += s
        total_items += i
        total_categories += c

    # 3. Final Summary
    free_now = shutil.disk_usage(os.path.expanduser("~")).free
    
    print("\n" + "=" * 60)
    status_text = "Scan complete (Preview)" if dry_run else "Cleanup complete"
    print(f"\033[1;34m{status_text}\033[0m")
    
    size_label = "Space that can be freed" if dry_run else "Space freed"
    print(f"{size_label}: \033[1;32m{bytes_to_human(total_size)}\033[0m | Items: {total_items} | Categories: {total_categories}")
    
    if not dry_run:
        # Fun fact: 4K movie estimate
        movies = total_size / (8 * 1024 * 1024 * 1024)
        if movies >= 0.1:
            print(f"Equivalent to ~{movies:.1f} 4K movies of storage.")
        print(f"Free space now: {bytes_to_human(free_now)}")
    
    print("=" * 60)
    
    if dry_run:
        print(f"\n{GRAY}ℹ️  Run without --dry-run to actually delete these files.{RESET}")


from .manage.uninstall import UninstallManager
from .manage.self_uninstall import run_self_uninstall
from .manage.install import run_install_link
from .ui.navigator import PaginatedSelector, UninstallSelector

# ANSI Colors

def run_purge(dry_run=False):
    while True:
        print("\033[1;95m➤ Project Purge\033[0m")
        manager = PurgeManager()
        results = manager.run_scan()
        
        if not results:
            print("✨ No heavy artifacts found. Your projects are clean!")
            input("\nPress Enter to return to menu...")
            return

        selector = PaginatedSelector("Select Project Artifacts to Purge", results)
        action = selector.run()
        
        if action == "MANAGE_PATHS":
            from .core.config import add_purge_path, remove_purge_path, get_purge_paths
            while True:
                os.system('clear')
                print("\n\033[1;36m⚙️  topo Purge Settings\033[0m")
                print("-" * 50)
                paths = get_purge_paths()
                print(f"Current Purge Search Paths:")
                for i, p in enumerate(paths):
                    print(f"  [{i+1}] {p}")
                
                print("\nOptions: [A] Add Path | [R] Remove Path | [B] Back to Scan")
                c = input("➤ ").lower()
                if c == 'a':
                    new_p = input("Enter new search path: ")
                    if add_purge_path(new_p): print(f"✅ Added: {new_p}")
                    input("\nPress Enter...")
                elif c == 'r':
                    try:
                        idx = int(input("Enter index to remove: ")) - 1
                        if 0 <= idx < len(paths):
                            remove_purge_path(paths[idx])
                            print(f"✅ Removed path.")
                        else: print("❌ Invalid index.")
                    except: print("❌ Invalid input.")
                    input("\nPress Enter...")
                elif c == 'b':
                    break
            continue # Re-scan with new paths

        if action and isinstance(action, list):
            selected = action
            if dry_run:
                total_size = sum(results[i]['size'] for i in selected)
                from .core.file_ops import bytes_to_human
                print(f"\n🧪 [DRY RUN] Would remove {len(selected)} items, freeing {bytes_to_human(total_size)}")
            else:
                count, total_freed = manager.execute_purge(selected)
                print(f"\n✨ Purge complete: {count} items removed, {total_freed} space freed.")
            input("\nPress Enter to return to menu...")
            break
        else:
            break

def run_uninstall():
    manager = UninstallManager()
    
    while True:
        apps = manager.run_full_scan()
        
        if not apps:
            print("\n   \033[1;31mNo applications found to uninstall.\033[0m")
            input("\nPress Enter to return to menu...")
            return

        selector = UninstallSelector("Select Application to Uninstall", apps)
        selected_indices = selector.run()
        
        if not selected_indices:
            return # Back to main menu

        # --- MOLE STYLE PREVIEW ---
        os.system('clear')
        total_estimated_size = 0
        from .core.file_ops import bytes_to_human
        
        print(f"\n \033[1;35m☉ Reviewing uninstallation plan:\033[0m\n")
        
        for idx in selected_indices:
            app = apps[idx]
            # Check if running for the [Running] tag
            is_running = False
            # Check keywords and some common process names
            procs_to_check = app.get('keywords', []) + [app['id'].split('.')[-1].lower(), app['name'].lower()]
            for proc in procs_to_check:
                if not proc: continue
                try:
                    res = subprocess.run(["pgrep", "-x", proc], capture_output=True)
                    if res.returncode == 0:
                        is_running = True; break
                except: pass
            
            running_tag = f" \033[1;33m[Running]\033[0m" if is_running else ""
            print(f"  \033[1;32m✓\033[0m {BOLD}{app['name']}{RESET}{running_tag}")
            total_estimated_size += app['size_bytes']
            
            # Show paths with Mole-style icons
            for p in app['data_paths']:
                if p.exists():
                    try:
                        rel_p = f"~/{p.relative_to(Path.home())}"
                        print(f"    \033[1;34m✓\033[0m {GRAY}{rel_p}{RESET}")
                    except:
                        print(f"    \033[1;34m✓\033[0m {GRAY}{p}{RESET}")
        
        # --- MOLE STYLE CONFIRMATION LINE ---
        app_text = "app" if len(selected_indices) == 1 else "apps"
        size_display = bytes_to_human(total_estimated_size)
        
        print(f"\n \033[1;35m→\033[0m Remove {len(selected_indices)} {app_text}, {size_display}  \033[1;32mEnter\033[0m confirm, \033[1;90mESC\033[0m cancel: ", end="", flush=True)

        # Capture single key
        import tty, termios
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(sys.stdin.fileno())
            ch = sys.stdin.read(1)
            # Handle ESC (\x1b)
            if ch == '\x1b':
                # Check if it's a sequence or just ESC
                # Non-blocking read to see if there's more
                import select
                if select.select([sys.stdin], [], [], 0)[0]:
                    ch = 'ESC_SEQ' # It's an arrow key or something else
                else:
                    ch = 'ESC'
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        
        if ch not in ('\r', '\n', 'y', 'Y'):
            print(f"\n\n {GRAY}Uninstallation cancelled.{RESET}")
            time.sleep(1)
            continue

        # --- EXECUTION ---
        total_freed_all = 0
        removed_names = []
        
        os.system('clear')
        print(f"\033[1;35m🚀 Executing uninstallation...\033[0m\n")

        for idx in selected_indices:
            app = apps[idx]
            print(f"\033[1;35m☉\033[0m {BOLD}{app['name']}{RESET}")
            
            success, freed_bytes, details = manager.uninstall_app(idx)
            if success:
                total_freed_all += freed_bytes
                removed_names.append(app['name'])
                for is_ok, path in details:
                    # Using icons and colors from the screenshot
                    if is_ok:
                        print(f"  \033[0;32m✓\033[0m {path}")
                    else:
                        # Non-file actions (like stopping processes)
                        print(f"  \033[1;35m☉\033[0m {path}")
                print(f"\033[0;32m[✓]\033[0m {app['name']} removed.\n")
            else:
                print(f"\033[1;31m[✗]\033[0m {app['name']} uninstallation failed.\n")

        # Final Summary (Pixel-perfect matching of the screenshot)
        print("=" * 70)
        print("\033[1;34mUninstall complete\033[0m")
        names_str = ", ".join(removed_names)
        print(f"Removed {len(removed_names)} app(s), freed \033[1;32m{bytes_to_human(total_freed_all)}\033[0m: {names_str}")
        print("=" * 70)

        # Match the specific prompt style from screenshot
        print(f"\n{GRAY}Press Enter to return to application list, any other key to exit... {RESET}", end="", flush=True)
        choice = input()
        if choice != "":
            break

from .core.whitelist import add_to_whitelist, remove_from_whitelist

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
  topo whitelist list  View currently protected paths
  topo --dry-run clean Preview files to be cleaned without deleting
"""
    )
    
    # Use a subparser for better help organization
    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    # --- Core Actions ---
    subparsers.add_parser("clean", help="One-key safe disk cleanup")
    subparsers.add_parser("purge", help="Interactive project artifact purging")
    subparsers.add_parser("uninstall", help="Completely remove applications and residues")
    subparsers.add_parser("optimize", help="Run system maintenance (fstrim, databases, etc.)")
    subparsers.add_parser("analyze", help="Interactive disk usage explorer")
    subparsers.add_parser("status", help="Monitor system health and resource usage")
    
    # --- Management ---
    subparsers.add_parser("link", help="Create a symbolic link in ~/.local/bin for 'topo' command")
    wl_parser = subparsers.add_parser("whitelist", help="Manage path protection whitelist")
    wl_parser.add_argument("action", choices=["add", "remove", "list"], nargs="?", default="list", help="Whitelist action")
    wl_parser.add_argument("path", nargs="?", help="Target path for add/remove")
    
    subparsers.add_parser("authorize", help="Setup passwordless sudo for faster cleanup")
    subparsers.add_parser("remove", help="Uninstall topo from the system")
    subparsers.add_parser("all", help="Run all cleanup and purge tasks sequentially")

    # --- Global Options ---
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without deleting")
    parser.add_argument("--version", action="version", version="topo 0.5.0")
    
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
                print("🔒 Authorizing optimization tasks...")
                ensure_sudo_session()
                optimize_system(args.dry_run)
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
    print(f"\033[1;34mtopo 0.5.0 (Python Edition)\033[0m")
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
        print("🔒 Authorizing optimization tasks...")
        ensure_sudo_session()
        optimize_system(args.dry_run)

    if args.command == "link":
        run_install_link()

    if args.command == "remove":
        run_self_uninstall(args.dry_run)

if __name__ == "__main__":
    main()
