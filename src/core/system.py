import os
import signal
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from .constants import BOLD, CLEAR_LINE, ERASE_BELOW, RESET, YELLOW

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


def get_os_id():
    try:
        if os.path.exists("/etc/os-release"):
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("ID="):
                        return line.strip().split("=")[1].strip('"')
    except (OSError, IndexError):
        pass
    return "unknown"


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
    # A sudoers NOPASSWD command path must be a single, unquoted token. Refuse to
    # emit a rule for paths with spaces/quotes (or a bogus invoking user) rather
    # than print one that is syntactically wrong or broader than intended.
    if not user or user == "unknown" or " " in script_path or "'" in script_path:
        print(
            "Could not generate a safe sudoers rule for this install "
            f"(user={user!r}, path={script_path!r})."
        )
        return

    rule = f"{user} ALL=(ALL) NOPASSWD: {script_path}"
    print("To allow topo to run without ever asking for a password, run this command once:")
    print(f"\n{YELLOW}echo '{rule}' | sudo tee /etc/sudoers.d/topo{RESET}\n")
    print("This will create a safe rule specifically for the topo script.")
