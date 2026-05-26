import os
import shutil
import subprocess
from pathlib import Path
from ..core.system import run_command
from ..core.file_ops import safe_remove, get_size

def is_app_running(process_name):
    """Check if an application is currently running to avoid cleaning active caches."""
    try:
        # Using pgrep for a reliable cross-distro check
        res = subprocess.run(["pgrep", "-x", process_name], capture_output=True)
        return res.returncode == 0
    except:
        return False

def clean_app_generic(name, paths, process_names=None, dry_run=False):
    """Helper for cleaning application caches with safety checks."""
    if process_names:
        for p in process_names:
            if is_app_running(p):
                print(f"  \033[0;90m◎\033[0m {name} is running · cleanup skipped")
                return 0, 0

    total_freed = 0
    items_cleaned = 0
    found = False
    
    for p_str in paths:
        path = Path(p_str).expanduser()
        if path.exists():
            found = True
            size = get_size(path)
            if dry_run:
                total_freed += size
                items_cleaned += 1
                continue
                
            # Try to clean contents
            try:
                if path.is_dir():
                    for item in path.iterdir():
                        s = get_size(item)
                        if safe_remove(item, use_trash=False)[0]:
                            total_freed += s
                            items_cleaned += 1
                else:
                    if safe_remove(path, use_trash=False)[0]:
                        total_freed += size
                        items_cleaned += 1
            except:
                continue
    
    if found and (total_freed > 0 or dry_run):
        status = "would be cleaned" if dry_run else "cache cleaned"
        size_info = f" ({get_size_str(total_freed)})" if total_freed > 0 else ""
        print(f"  \033[0;32m✓\033[0m {name}{size_info} {status}")
        return total_freed, items_cleaned
    return 0, 0

def get_size_str(b):
    from ..core.file_ops import bytes_to_human
    return bytes_to_human(b)

def clean_flatpak_unused(dry_run=False):
    """Removes unused Flatpak runtimes which can be very large."""
    if shutil.which("flatpak"):
        if dry_run:
            print("  \033[0;32m✓\033[0m Flatpak runtimes would be checked")
            return 0, 0
            
        # Capture to see if something was actually done
        res = run_command(["flatpak", "uninstall", "--unused", "-y"], use_sudo=False, capture=True)
        if res and res.stdout:
            # Simple heuristic: if it mentions 'Uninstalling', something was done
            if "Uninstalling" in res.stdout:
                from .system import parse_size_from_text
                freed = parse_size_from_text(res.stdout)
                print(f"  \033[0;32m✓\033[0m Cleaned unused Flatpak runtimes" + (f" ({get_size_str(freed)})" if freed > 0 else ""))
                return freed, 1
    return 0, 0

def clean_apps_deep(dry_run=False):
    print("\033[1;95m➤ Deep App Cleanup\033[0m")
    total_size = 0
    total_items = 0
    total_categories = 0

    # 1. Communications & Social
    apps = [
        ("Discord", ["~/.config/discord/Cache", "~/.config/discord/Code Cache", "~/.config/discord/GPUCache"], ["discord"]),
        ("Telegram", ["~/.local/share/TelegramDesktop/tdata/user_data/Cache", "~/.local/share/TelegramDesktop/tdata/user_data/temp"], ["Telegram"]),
        ("Slack", ["~/.config/Slack/Cache", "~/.config/Slack/Service Worker/CacheStorage"], ["slack"]),
        ("Zoom", ["~/.zoom/data"], ["zoom"]),
        ("Microsoft Teams", ["~/.config/Microsoft/Teams/Cache", "~/.config/Microsoft/Teams/Application Cache"], ["teams"]),
        ("WeChat (Wine/Flatpak)", ["~/.var/app/com.tencent.WeChat/cache"], None),
    ]
    
    for name, paths, procs in apps:
        s, i = clean_app_generic(name, paths, procs, dry_run=dry_run)
        total_size += s; total_items += i; total_categories += (1 if i > 0 else 0)

    # 2. Multimedia
    media_apps = [
        ("Spotify", ["~/.cache/spotify/Data"], ["spotify"]),
        ("VLC", ["~/.cache/vlc"], ["vlc"]),
        ("OBS Studio", ["~/.config/obs-studio/logs", "~/.config/obs-studio/crashes"], ["obs"]),
    ]
    for name, paths, procs in media_apps:
        s, i = clean_app_generic(name, paths, procs, dry_run=dry_run)
        total_size += s; total_items += i; total_categories += (1 if i > 0 else 0)

    # 3. Browsers (Beyond standard cache)
    browsers = [
        ("Google Chrome (System)", ["~/.config/google-chrome/Default/Cache", "~/.config/google-chrome/Default/Code Cache"], ["google-chrome"]),
        ("Brave Browser", ["~/.config/BraveSoftware/Brave-Browser/Default/Cache"], ["brave"]),
        ("Microsoft Edge", ["~/.config/microsoft-edge/Default/Cache"], ["microsoft-edge"]),
    ]
    for name, paths, procs in browsers:
        s, i = clean_app_generic(name, paths, procs, dry_run=dry_run)
        total_size += s; total_items += i; total_categories += (1 if i > 0 else 0)

    # 4. System Packaging
    s, i = clean_flatpak_unused(dry_run=dry_run)
    total_size += s; total_items += i; total_categories += (1 if i > 0 else 0)

    return total_size, total_items, total_categories
