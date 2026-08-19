import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import terminal_state
from .constants import (
    BOLD,
    CLEAR_LINE,
    ERASE_BELOW,
    GRAY,
    GREEN,
    PURPLE,
    RED,
    RESET,
    YELLOW,
)

_SAFE_USERNAME_RE = re.compile(r"[a-z_][a-z0-9_-]{0,31}\$?\Z")
_SAFE_SUDOERS_PATH_RE = re.compile(r"/[A-Za-z0-9._+/-]*\Z")

# Global flag to track if user explicitly cancelled sudo auth
SUDO_CANCELLED = False
DEFAULT_COMMAND_TIMEOUT = 300
SUDO_INTERRUPT_EXTRA_CLEAR_LINES = 8


@dataclass
class CommandResult:
    args: list[str]
    returncode: int
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.error


def get_os_info():
    """Return tuple of (id, id_like) from /etc/os-release."""
    os_id = "unknown"
    id_like = ""
    try:
        if os.path.exists("/etc/os-release"):
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("ID="):
                        os_id = line.strip().split("=")[1].strip('"').lower()
                    elif line.startswith("ID_LIKE="):
                        id_like = line.strip().split("=")[1].strip('"').lower()
    except (OSError, IndexError):
        pass
    return os_id, id_like


def get_os_id():
    return get_os_info()[0]


def is_os_family(family: str) -> bool:
    """Check if current OS matches family by ID or ID_LIKE."""
    os_id, id_like = get_os_info()
    fam = family.lower()
    return fam in os_id or fam in id_like.split()


def get_invoking_user():
    return os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown"


def run_command(args: list[str], use_sudo=False, capture=True, timeout=DEFAULT_COMMAND_TIMEOUT):
    cmd = (["sudo", "-n"] + args if SUDO_CANCELLED else ["sudo"] + args) if use_sudo else args

    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=False,
            timeout=timeout,
        )
        return CommandResult(
            args=cmd,
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )
    except subprocess.TimeoutExpired as e:
        return CommandResult(
            args=cmd,
            returncode=124,
            stdout=_decode_output(e.stdout),
            stderr=_decode_output(e.stderr),
            error=f"Command timed out after {timeout}s",
            timed_out=True,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return CommandResult(args=cmd, returncode=127, error=str(e))


def _decode_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def has_sudo():
    """Check if current user has active sudo session"""
    res = run_command(["-n", "true"], use_sudo=True)
    return res.ok


def authenticate_sudo_session(dry_run: bool, *, request_subject: str, action: str) -> bool:
    """Ask for consent and pre-authorize sudo for a named operation."""
    if dry_run:
        return True

    action_title = action.capitalize()
    print(
        f"{PURPLE}➔{RESET} {request_subject} need sudo. "
        f"{GREEN}Enter{RESET} continue, {GRAY}Space{RESET} skip:",
        end=" ",
        flush=True,
    )
    choice = terminal_state.read_sudo_choice()
    print()
    if choice in (" ", "\x1b"):
        return False

    if not ensure_sudo_session(
        f"{PURPLE}➔{RESET} System {action} requires admin access\n{PURPLE}➔{RESET} Password: "
    ):
        if SUDO_CANCELLED:
            print(f" {YELLOW}⚠️  {action_title} cancelled by user.{RESET}", end="")
        else:
            print(f" {RED}✗{RESET} Authorization failed. {action_title} skipped.\n")
        return False

    print(f"{GREEN}ꗃ{RESET} Authorization successful.\n")
    return True


def ensure_sudo_session(prompt: str | None = None):
    """Force a fresh sudo password prompt by invalidating cached credentials."""
    global SUDO_CANCELLED
    SUDO_CANCELLED = False  # Reset for each attempt

    try:
        # 1. Invalidate the current user's cached credentials (force prompt)
        run_command(["-k"], use_sudo=True, capture=True, timeout=10)

        # 2. Check if a permanent NOPASSWD rule exists first
        if run_command(["-n", "true"], use_sudo=True, capture=True, timeout=10).ok:
            return True

        # 3. sudo -v (validate) asks for the password and updates the timestamp
        validate_args = ["-v"]
        if prompt:
            validate_args.extend(["-p", prompt])
        res = run_command(validate_args, use_sudo=True, capture=False, timeout=None)
        if res.returncode in (-signal.SIGINT, 128 + signal.SIGINT):
            _clear_interrupted_sudo_prompt(prompt)
            SUDO_CANCELLED = True
        return res.ok
    except KeyboardInterrupt:
        _clear_interrupted_sudo_prompt(prompt)
        SUDO_CANCELLED = True
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def _clear_interrupted_sudo_prompt(prompt: str | None = None) -> None:
    prompt_lines = prompt.count("\n") + 1 if prompt else 1
    lines_to_rewind = prompt_lines + SUDO_INTERRUPT_EXTRA_CLEAR_LINES
    clear_sequence = CLEAR_LINE + (f"\033[1A{CLEAR_LINE}" * lines_to_rewind) + ERASE_BELOW
    try:
        sys.stdout.write(clear_sequence)
        sys.stdout.flush()
    except (OSError, ValueError):
        return


def setup_passwordless_sudo():
    """Generate a command to enable permanent passwordless sudo for the current user."""
    user = get_invoking_user()
    script_path = os.path.realpath(sys.argv[0])

    print(f"\n{BOLD}🛡️  Setup Passwordless Mode{RESET}")

    if not user or user == "unknown" or not _SAFE_USERNAME_RE.match(user):
        print(
            f"{YELLOW}⚠️  Could not determine a safe username; refusing to generate a sudoers rule.{RESET}"
        )
        return

    if not _SAFE_SUDOERS_PATH_RE.match(script_path):
        print(
            f"{YELLOW}⚠️  Could not generate a safe sudoers rule for path with special characters or spaces: {script_path!r}{RESET}"
        )
        return

    # Check if script is owned by root and not writable by non-root users
    script_p = Path(script_path)
    is_user_writable = False
    for p in [script_p, *script_p.parents]:
        try:
            st = p.lstat()
            if st.st_uid != 0 or (st.st_mode & 0o022):
                is_user_writable = True
                break
        except OSError:
            is_user_writable = True
            break

    if is_user_writable:
        print(
            f"{YELLOW}⚠️  Refusing NOPASSWD rule for script at {script_path}:{RESET}\n"
            f"  This script is user-writable. Granting NOPASSWD to user-writable scripts allows local privilege escalation.\n"
        )
        print(
            "To allow passwordless maintenance safely, grant NOPASSWD for specific binaries with strict parameters instead:"
        )
        print(
            f"\n{YELLOW}echo '{user} ALL=(root) NOPASSWD: /usr/sbin/fstrim -a, /usr/bin/journalctl --vacuum-time=3d' | sudo tee /etc/sudoers.d/topo{RESET}\n"
        )
        return

    rule = f"{user} ALL=(ALL) NOPASSWD: {script_path}"
    print("To allow topo to run without ever asking for a password, run this command once:")
    print(f"\n{YELLOW}echo '{rule}' | sudo tee /etc/sudoers.d/topo{RESET}\n")
    print("This will create a specific rule for the system-installed topo binary.")
