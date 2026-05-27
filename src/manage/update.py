import subprocess
import os
from ..core.constants import CYAN, GRAY, RESET, BOLD, RED

def run_update():
    """Updates topo by re-running the official installation script."""

    # We leverage the existing install.sh which is architecture-aware and professional
    install_cmd = "curl -fsSL https://raw.githubusercontent.com/Jesencloud/Topo/main/install.sh | bash"
    
    try:
        # Use shell=True to handle the pipe directly
        process = subprocess.run(install_cmd, shell=True)
        
        if process.returncode != 0:
            print(f"\n {RED}❌ Update failed with exit code {process.returncode}{RESET}")
            
    except Exception as e:
        print(f"\n {RED}❌ Error during update: {e}{RESET}")
