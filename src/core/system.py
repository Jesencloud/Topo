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

# Every output parser in Topo matches English words ("Uninstalling", "disabled",
# "Total reclaimed space") and English unit suffixes, so any command whose stdout
# is read rather than shown has to be asked for the C locale -- otherwise a
# zh_CN, de_DE or fr_FR desktop silently parses nothing. All three variables are
# pinned because LC_ALL outranks LC_MESSAGES and LANG, while LANGUAGE (a GNU
# gettext extension) outranks both for message catalogs. sudo's default env_keep
# passes LANG/LANGUAGE/LC_* through, so this survives use_sudo=True as well.
C_LOCALE_ENV = {"LC_ALL": "C", "LANGUAGE": "C", "LANG": "C"}

# A deb package's prerm/postrm may ask debconf a question. run_command captures
# output but leaves stdin attached, so the prompt would be swallowed while the
# terminal shows a spinner and the removal would sit there until the command
# timeout expires. noninteractive makes debconf take the defaults instead.
APT_NONINTERACTIVE_ENV = {**C_LOCALE_ENV, "DEBIAN_FRONTEND": "noninteractive"}


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


def run_command(
    args: list[str],
    use_sudo=False,
    capture=True,
    timeout=DEFAULT_COMMAND_TIMEOUT,
    env: dict[str, str] | None = None,
    # Opt-in only: a child that inherits our stdin can read the keystrokes a TUI
    # screen is waiting for. xdg-open needs it detached because with no desktop
    # session it falls through to a terminal handler (sensible-browser -> w3m),
    # which would then fight the caller for the keyboard until the timeout.
    # Interactive children -- above all `sudo` asking for a password -- must keep
    # stdin, which is why this cannot become the default.
    detach_stdin: bool = False,
):
    cmd = (["sudo", "-n"] + args if SUDO_CANCELLED else ["sudo"] + args) if use_sudo else args
    # Overlay rather than replace: dropping PATH, HOME or DISPLAY would break the
    # very tools being called. Pass C_LOCALE_ENV here whenever the output is
    # parsed instead of displayed.
    child_env = {**os.environ, **env} if env else None

    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            check=False,
            timeout=timeout,
            env=child_env,
            stdin=subprocess.DEVNULL if detach_stdin else None,
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
    global SUDO_CANCELLED
    if dry_run:
        return True

    action_title = action.capitalize()
    print(
        f"{PURPLE}➔{RESET} {request_subject} need sudo. "
        f"{GREEN}Enter{RESET} continue, {GREEN}Space {RESET}or{GREEN} ESC{RESET} cancel:",
        end=" ",
        flush=True,
    )
    try:
        choice = terminal_state.read_sudo_choice()
    except KeyboardInterrupt:
        SUDO_CANCELLED = True
        print()
        return False
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


def setup_passwordless_sudo() -> bool:
    """Print the sudoers rule to enable passwordless sudo; False when it refused.

    Every refusal path prints a ⚠️ and produces no usable rule for this script,
    so a caller that pipes the output somewhere has to be able to tell. The
    user-writable case still prints a *different*, safe rule as advice -- it is
    deliberately still a failure: the rule the caller asked for was refused.
    """
    user = get_invoking_user()
    script_path = os.path.realpath(sys.argv[0])

    print(f"\n{BOLD}🛡️  Setup Passwordless Mode{RESET}")

    if not user or user == "unknown" or not _SAFE_USERNAME_RE.match(user):
        print(
            f"{YELLOW}⚠️  Could not determine a safe username; refusing to generate a sudoers rule.{RESET}"
        )
        return False

    if not _SAFE_SUDOERS_PATH_RE.match(script_path):
        print(
            f"{YELLOW}⚠️  Could not generate a safe sudoers rule for path with special characters or spaces: {script_path!r}{RESET}"
        )
        return False

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
        return False

    rule = f"{user} ALL=(ALL) NOPASSWD: {script_path}"
    print("To allow topo to run without ever asking for a password, run this command once:")
    print(f"\n{YELLOW}echo '{rule}' | sudo tee /etc/sudoers.d/topo{RESET}\n")
    print("This will create a specific rule for the system-installed topo binary.")
    return True
