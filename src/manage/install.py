import os
from pathlib import Path

from ..core.constants import (
    BOLD,
    FAIL,
    GRAY,
    INFO,
    MARK_SECTION,
    OK,
    PURPLE,
    RESET,
    TOPO_RC_MARKER,
    WARN,
    YELLOW,
)
from ..core.paths import get_link_target_dir


def run_install_link(silent=False):
    """Creates a symbolic link for the topo launcher in a PATH-friendly bin dir.

    Three steps, each its own function below: make the directory, put the link in
    it, then make sure the shell can find it. Any of the first two failing stops
    the install -- a link in a directory that could not be created, or a PATH
    entry pointing at a link that was never made, would both report success for a
    `topo` command that does not run.
    """

    if not silent:
        print(f"\n{PURPLE}{MARK_SECTION} Setting up system-wide 'topo' command...{RESET}")

    repo_root = Path(__file__).parent.parent.parent
    source_script = repo_root / "topo"
    target_dir = get_link_target_dir()
    target_link = target_dir / "topo"

    if not source_script.exists():
        if not silent:
            print(f"  {FAIL} Error: Could not find launcher script at {source_script}")
        return False

    if not _ensure_link_dir_exists(target_dir, silent):
        return False

    if not _link_launcher_atomically(source_script, target_link, silent):
        return False

    usable_now = _ensure_link_dir_on_path(target_dir, silent)

    if not silent and usable_now:
        print(
            f"  {OK} {GRAY}System setup complete. '{BOLD}topo{RESET}{GRAY}' is ready to use!{RESET}"
        )

    return True


def _ensure_link_dir_exists(target_dir: Path, silent: bool) -> bool:
    """Create the bin directory the link goes in, if it is not already there."""
    if target_dir.exists():
        return True
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        if not silent:
            disp_dir = str(target_dir).replace(str(Path.home()), "~")
            print(f"  {OK} {GRAY}Created directory {BOLD}{disp_dir}{RESET}")
    except OSError as e:
        if not silent:
            print(f"  {FAIL} Error creating directory {target_dir}: {e}")
        return False
    return True


def _link_launcher_atomically(source_script: Path, target_link: Path, silent: bool) -> bool:
    """Point the `topo` command at this tree's launcher, via a temp symlink + os.replace.

    Atomically, so an interrupted update never leaves the `topo` command missing:
    the replace either has the old link or the new one, never neither. A plain
    unlink-then-symlink has a window in between where the command does not exist.
    """
    try:
        tmp_link = target_link.with_name(f".{target_link.name}.topo-tmp")
        if tmp_link.exists() or tmp_link.is_symlink():
            tmp_link.unlink()
        tmp_link.symlink_to(source_script.absolute())
        os.replace(tmp_link, target_link)
        if not silent:
            disp_link = str(target_link).replace(str(Path.home()), "~")
            print(f"  {OK} {GRAY}Executable linked to {BOLD}{disp_link}{RESET}")
    except OSError as e:
        if not silent:
            print(f"  {FAIL} Error creating symbolic link: {e}")
            print(f"  {GRAY}You can still run topo directly with: {BOLD}{source_script}{RESET}")
        return False
    return True


def _ensure_link_dir_on_path(target_dir: Path, silent: bool) -> bool:
    """Make the link's directory reachable as a command; True when it already is.

    The auto-fix runs even in silent mode -- `topo install` calls this with
    silent=True, and a PATH entry it declined to add is a `topo` command the user
    cannot type. What silent suppresses is the reporting, not the repair.

    Returns whether the shell can find the command, now or after the user reloads
    their rc file: in PATH already, or an export line that is now in one of their
    shell configs. False means every write failed and the caller must not claim
    the install is ready to use.
    """
    path_env = os.environ.get("PATH", "")
    in_path = str(target_dir) in path_env.split(os.pathsep)
    added = False
    configured = False

    if not in_path:
        if not silent:
            print(
                f"\n {INFO} {GRAY}{target_dir} is not in your PATH. Attempting auto-fix...{RESET}"
            )

        shell_configs = [Path.home() / ".bashrc", Path.home() / ".zshrc"]
        if target_dir == Path.home() / ".local" / "bin":
            export_line = 'export PATH="$HOME/.local/bin:$PATH"'
        else:
            export_line = f'export PATH="{target_dir}:$PATH"'

        for config in shell_configs:
            if not config.exists():
                continue
            try:
                # errors="replace" is enough *here*, unlike the matching read in
                # remove.py: nothing decoded is ever written back, so a byte the
                # codec cannot handle can only hide the export line from the
                # containment test below -- never end up in the file as U+FFFD.
                # Strict decoding used to raise UnicodeDecodeError on any rc file
                # with a GBK or Latin-1 comment in it, and that is a ValueError,
                # which `except OSError` does not catch: `topo link` ended in a
                # traceback on a machine carried over from a pre-UTF-8 locale.
                content = config.read_text(errors="replace")
                if export_line in content:
                    configured = True
                    continue
                # Appended rather than rewritten, so the bytes already in the file
                # are never decoded and re-encoded.
                #
                # surrogateescape on the way out because export_line carries an
                # installation path: TOPO_LINK_DIR (and $HOME) reach Python
                # already decoded with surrogateescape, so a directory whose name
                # is not valid UTF-8 arrives as lone surrogates. Encoding those
                # strictly raises UnicodeEncodeError -- a ValueError, so the
                # `except OSError` below misses it exactly the way it missed the
                # decode -- while surrogateescape writes the path's original bytes
                # back, which is what bash needs to find the directory again.
                with open(config, "a", encoding="utf-8", errors="surrogateescape") as f:
                    f.write(f"\n{TOPO_RC_MARKER}\n{export_line}\n")
                if not silent:
                    print(f"  {OK} Added to {GRAY}{config.name}{RESET}")
                added = True
                configured = True
            except OSError:
                pass

        if not silent:
            if added:
                print(f"\n {BOLD}Please restart your terminal or run:{RESET}")
                print(f" {GRAY}source ~/.bashrc{RESET} (or your shell config)")
            elif configured:
                print(f"\n {GRAY}PATH configuration already exists in your shell config.{RESET}")
                print(f" {BOLD}Please restart your terminal or run:{RESET}")
                print(f" {GRAY}source ~/.bashrc{RESET} (or your shell config)")
            else:
                print(f"\n {WARN} {YELLOW}Manual action required:{RESET}")
                print(" Add this line to your .bashrc or .zshrc:")
                print(f" {GRAY}{export_line}{RESET}")

    return in_path or configured
