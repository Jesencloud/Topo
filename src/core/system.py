import os
import subprocess
import shutil
import sys
from pathlib import Path

# Global flag to track if user explicitly cancelled sudo auth
SUDO_CANCELLED = False

def get_os_id():
    try:
        if os.path.exists("/etc/os-release"):
            with open("/etc/os-release", "r") as f:
                for line in f:
                    if line.startswith("ID="):
                        return line.strip().split("=")[1].strip('"')
    except Exception:
        pass
    return "unknown"

def get_invoking_user():
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown"

def run_command(args, use_sudo=False, capture=True):
    if use_sudo:
        if SUDO_CANCELLED:
            cmd = ["sudo", "-n"] + args
        else:
            cmd = ["sudo"] + args
    else:
        cmd = args
        
    try:
        result = subprocess.run(
            cmd, 
            capture_output=capture, 
            text=True, 
            check=False
        )
        return result
    except Exception:
        return None

import threading
import time

# ANSI Colors for Setup Output
BOLD = "\033[1m"
RESET = "\033[0m"

def has_sudo():
    """Check if current user has active sudo session"""
    res = run_command(["-n", "true"], use_sudo=True)
    return res and res.returncode == 0

def ensure_sudo_session():
    """Force a fresh sudo password prompt by invalidating cached credentials."""
    global SUDO_CANCELLED
    
    try:
        # 1. Invalidate the current user's cached credentials (force prompt)
        subprocess.run(["sudo", "-k"], capture_output=True)
        
        # 2. Check if a permanent NOPASSWD rule exists first
        # If this succeeds AFTER -k, it means they truly have NOPASSWD configured
        if run_command(["-n", "true"], use_sudo=True, capture=True).returncode == 0:
            return True
        
        # 3. sudo -v (validate) asks for the password and updates the timestamp
        res = subprocess.run(["sudo", "-v"], capture_output=False)
        if res.returncode != 0:
            SUDO_CANCELLED = True
        return res.returncode == 0
    except KeyboardInterrupt:
        print() # Add a newline after ^C
        SUDO_CANCELLED = True
        return False
    except:
        SUDO_CANCELLED = True
        return False

def setup_passwordless_sudo():
    """Generate a command to enable permanent passwordless sudo for the current user."""
    user = os.getenv("USER")
    script_path = os.path.realpath(sys.argv[0])
    rule = f"{user} ALL=(ALL) NOPASSWD: {script_path}"
    
    print(f"\n{BOLD}🛡️  Setup Passwordless Mode{RESET}")
    print(f"To allow topo to run without ever asking for a password, run this command once:")
    print(f"\n\033[1;33mecho '{rule}' | sudo tee /etc/sudoers.d/topo\033[0m\n")
    print(f"This will create a safe rule specifically for the topo script.")
