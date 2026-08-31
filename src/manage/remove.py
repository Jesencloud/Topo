import contextlib
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

from ..core import system, terminal_state
from ..core.constants import (
    BOLD,
    FAIL,
    GRAY,
    GREEN,
    MAGENTA,
    MARK_PROMPT,
    MARK_SECTION,
    OK,
    PURPLE,
    RESET,
    TOPO_RC_MARKER,
    WARN,
)
from ..core.file_ops import bytes_to_human, get_size_fast, safe_remove
from ..core.install_source import (
    PACKAGE_INSTALL,
    get_install_source,
    get_package_remove_argv,
)
from ..core.lock import LOCK_FILE_PATH
from ..core.paths import get_config_dir, get_launcher_candidates, get_state_dir


class _RemoveItem(TypedDict, total=False):
    path: Path
    desc: str
    type: str
    size: int


def _resolve_launcher_symlink(launcher: Path) -> Path | None:
    """Resolve a launcher symlink to its real target, or None on failure."""
    try:
        raw = Path(os.readlink(launcher))
        return (raw if raw.is_absolute() else launcher.parent / raw).resolve()
    except (OSError, ValueError):
        return None


def _launcher_points_to_topo(launcher_path: Path, internal_dir: Path) -> bool:
    """True if the launcher is Topo's link, even when dangling (target removed)."""
    try:
        expected = (internal_dir / "topo").resolve()
    except OSError:
        expected = internal_dir / "topo"
    if launcher_path.is_symlink():
        resolved = _resolve_launcher_symlink(launcher_path)
        if not resolved:
            # Fallback for dangling symlink string match
            raw = Path(os.readlink(launcher_path))
            target = raw if raw.is_absolute() else launcher_path.parent / raw
            return os.path.normpath(target) == os.path.normpath(expected)
        return resolved == expected or str(resolved) == str(expected)
    try:
        return launcher_path.resolve() == expected
    except (OSError, UnicodeDecodeError):
        return False


def _replace_file_bytes(path: Path, data: bytes) -> bool:
    """Put *data* in *path* atomically, keeping the file the user's own.

    The same temp-file-then-os.replace shape as core/json_store.py's writer, for
    the same reason: ``write_text`` truncates before the first byte of the
    replacement is written, so a full disk or a killed process left an empty
    shell config where a working one used to be. Two things json_store never has
    to think about, because the files it writes are topo's:

    A dotfile manager (stow, chezmoi, yadm) makes ~/.bashrc a symlink into its
    own repository. Renaming over the symlink would leave a regular file in its
    place and quietly cut the file bash reads out of that repository, so the
    rename targets whatever the link resolves to.

    And the mode is the original's, taken before the write and restored after it,
    rather than the 0o666 & ~umask a new file would get -- otherwise a .bashrc
    someone had deliberately kept private comes back world-readable. When topo
    runs as root over another user's home the owner is put back too, best effort:
    only root may give a file away, and a failure there is not worth losing the
    edit over.

    fsync covers the contents; the directory entry is deliberately not synced.
    A rename lost to a power cut leaves the previous, intact rc file in place,
    which costs one stale PATH line -- json_store guards a whitelist, where the
    same loss would be a protection the user thought they had.
    """
    target = path
    if path.is_symlink():
        try:
            target = path.resolve(strict=True)
        except OSError:
            return False
    tmp = target.with_name(f".{target.name}.topo-tmp-{os.getpid()}")
    try:
        original = target.stat()
        mode = stat.S_IMODE(original.st_mode)
        # Created with the final mode rather than chmod'ed into it afterwards:
        # umask can only clear bits, so the scratch file is never wider than the
        # file it replaces, not even for the moment it holds the whole rc file.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with open(fd, "wb", closefd=True) as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        with contextlib.suppress(OSError):
            os.chown(tmp, original.st_uid, original.st_gid)
        os.replace(tmp, target)
    except OSError:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        return False
    return True


def _strip_topo_path_lines() -> bool:
    """Remove the `# Added by topo` PATH-export block from shell rc files.

    Read and written as bytes, and both markers spelled as bytes, because this
    file belongs to the user. ``read_text()`` decodes strictly, and a .bashrc
    carrying a GBK or Latin-1 comment -- ordinary on a machine brought over from
    a pre-UTF-8 locale -- raises UnicodeDecodeError, a ValueError that the
    ``except OSError`` here could not catch. The traceback landed *after* the
    loop in _remove_package_user_residue() had deleted the install directory, the
    config and whitelist, the scan cache and the deletion history: those are
    gone, the PATH line is still there, and the `removed` report never prints.

    ``errors="replace"`` would trade the crash for a corrupted file -- every byte
    it could not decode written back as U+FFFD, into the user's shell config.
    Nothing here needs the text: both markers are ASCII, so the bytes match
    exactly and every other line is copied through untouched, whatever it holds.
    """
    # The marker comes from constants so that the command writing it and the
    # command taking it back out cannot drift apart; ASCII, so .encode() is exact.
    marker = TOPO_RC_MARKER.encode()
    export_prefix = b"export PATH="
    changed = False
    for config in (Path.home() / ".bashrc", Path.home() / ".zshrc"):
        if not config.exists():
            continue
        try:
            original = config.read_bytes().splitlines()
        except OSError:
            continue
        cleaned: list[bytes] = []
        drop_export = False
        for line in original:
            if line.strip() == marker:
                drop_export = True
                continue
            if drop_export and line.strip().startswith(export_prefix):
                drop_export = False
                continue
            drop_export = False
            cleaned.append(line)
        if cleaned != original and _replace_file_bytes(
            config, b"\n".join(cleaned) + (b"\n" if cleaned else b"")
        ):
            changed = True
    return changed


def _launcher_points_to_package(launcher_path: Path) -> bool:
    if launcher_path.is_symlink():
        resolved = _resolve_launcher_symlink(launcher_path)
        if not resolved:
            return False
        return str(resolved) in {
            os.path.normpath("/usr/bin/topo"),
            os.path.normpath("/usr/lib/topo/topo"),
        }
    try:
        return (
            launcher_path.is_file()
            and "Managed by topo package compatibility launcher"
            in launcher_path.read_text(errors="replace")
        )
    except OSError:
        return False


def _config_dir_is_lock_only(config_dir: Path) -> bool:
    """True when ~/.config/topo contains nothing but this run's own lock file.

    `topo remove` is itself a lock-holding command, so by the time it looks
    around, SingleInstanceLock has already created ~/.config/topo/topo.lock.
    Counting that as leftover configuration would mean `topo remove` could never
    report a clean system: run it twice and the second run would offer to delete
    the directory the second run just made. An empty directory is still treated
    as removable residue, exactly as before the lock covered this command.
    """
    try:
        return [entry.name for entry in config_dir.iterdir()] == [LOCK_FILE_PATH.name]
    except OSError:
        # Unreadable is not lock-only; let the normal removal path try and report.
        return False


def _remove_path(path: Path) -> bool:
    ok, _ = safe_remove(
        path,
        use_trash=False,
        allow_app_data_removal=True,
        allow_self_removal=True,
    )
    return ok


def _remove_package_user_residue() -> list[str]:
    removed: list[str] = []
    home = Path.home()
    internal_dir = home / ".topo"

    # Every candidate, not just ~/.local/bin: a compatibility launcher created by
    # `topo link` under TOPO_LINK_DIR (or by `sudo topo link`, in /usr/local/bin)
    # would otherwise survive the package removal as a dangling symlink. Not
    # any()/short-circuited on purpose -- more than one can exist, and stopping at
    # the first would leave the others. Paths this user cannot write simply fail
    # to be removed and stay unreported.
    launcher_removed = False
    for launcher_path in get_launcher_candidates():
        if (
            (launcher_path.exists() or launcher_path.is_symlink())
            and (
                _launcher_points_to_topo(launcher_path, internal_dir)
                or _launcher_points_to_package(launcher_path)
            )
            and _remove_path(launcher_path)
        ):
            launcher_removed = True
    if launcher_removed:
        removed.append("Launcher compatibility entry")

    config_dir = get_config_dir()
    # This run holds the instance lock inside ~/.config/topo, so the directory
    # always exists by the time we get here. Deleting it is still right -- an
    # empty directory holding nothing but our own lock file is residue this run
    # created -- but announcing it as removed configuration would be a lie on a
    # machine that never had any.
    config_is_lock_only = _config_dir_is_lock_only(config_dir)

    for path, label in (
        (internal_dir, "Script install directory"),
        (config_dir, "Configuration and whitelist"),
        (home / ".cache/topo", "Temporary scan cache"),
        (get_state_dir(), "Deletion history / state"),
    ):
        if _remove_path(path) and not (path == config_dir and config_is_lock_only):
            removed.append(label)

    if _strip_topo_path_lines():
        removed.append("Shell PATH entry")

    return removed


def _confirm_removal(prompt: str, assume_yes: bool = False) -> bool:
    """Ask for a single-key confirmation; False when declined or non-interactive.

    Deliberately not terminal_state.read_sudo_choice(): that helper treats a
    non-TTY stdin as "\\n" -- accept -- which is a sane default for a sudo prompt
    and exactly the wrong one for "delete the installation". With no terminal
    there is nobody to confirm, so refuse. Without the guard the read below
    raises termios.error, which is *not* an OSError subclass, so no
    `except OSError` on the way up catches it and the topo launcher (which only
    handles ImportError and KeyboardInterrupt) lets it print a traceback -- the
    outcome for `ssh host topo remove`, containers, and any pipe.

    `assume_yes` (`topo remove --yes`) is the deliberate escape hatch for the
    callers that legitimately have no terminal: CI smoke tests, container image
    builds, provisioning tools. Refusing without offering one would only push
    them back to `apt remove topo`, which skips the user-residue cleanup this
    command exists for. It is checked before isatty so `--yes` works on a pipe.

    Both install sources come through here. The package manager is invoked with
    its own -y, so this prompt is topo's only confirmation on that path; leaving
    it out made `topo remove` uninstall a packaged topo with no question asked
    while the script install asked for Enter.
    """
    if assume_yes:
        return True

    if not sys.stdin.isatty():
        print(f"\n {WARN} Removing topo needs an interactive confirmation.")
        print(
            f"  {GRAY}Run it from a terminal, pass{RESET} {BOLD}--yes{RESET}{GRAY} to skip this "
            f"prompt, or preview with{RESET} {BOLD}topo remove --dry-run{RESET}{GRAY}.{RESET}"
        )
        return False

    print(prompt, end="", flush=True)

    # Single-key capture
    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    terminal_state.remember_raw_state(fd, old_settings)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        terminal_state.restore_raw_state(fd, old_settings)

    if ch not in ("\r", "\n", "y", "Y"):
        # The prompt was printed with end="" and answered in raw mode, so the
        # cursor is still sitting at the end of it: one newline closes that line,
        # the second keeps the blank line this notice has always had above it.
        print("\n")
        system.print_action_cancelled("Uninstallation")
        return False
    return True


def run_remove(dry_run=False, assume_yes=False) -> bool:
    """Remove topo from the system; False when the removal did not fully happen.

    A cancelled confirmation and a non-TTY refusal are failures for the same
    reason a delete error is: topo is still installed, so `topo remove && ...`
    must not continue. "Nothing to remove" is a success -- the system is already
    in the state the caller asked for.
    """

    if get_install_source() == PACKAGE_INSTALL:
        command = get_package_remove_argv()
        if not command:
            print(f"\n {FAIL} Unsupported Linux distribution for package removal.")
            return False
        print(
            f"\n {MAGENTA}{MARK_SECTION} Removing Topo through the system package manager.{RESET}\n"
        )
        # "Command:" and not "Running:": this line is printed before the
        # confirmation, so on the --dry-run and declined paths nothing runs.
        print(f" {GRAY}Command:{RESET} {BOLD}{' '.join(command)}{RESET}")
        if dry_run:
            print(f" {OK} Dry run complete. Package removal command was not executed.")
            return True
        if not _confirm_removal(
            f"\n {PURPLE}{MARK_PROMPT}{RESET} Remove the topo package: "
            f"Press {GREEN}Enter{RESET} confirm, {GREEN}ESC{RESET} cancel: ",
            assume_yes,
        ):
            return False
        print()
        try:
            # No deadline, like every other package transaction: subprocess.run
            # SIGKILLs the child when one expires, and what dies here is the
            # package manager removing topo -- mid-transaction, leaving dpkg to be
            # repaired by hand. The command is non-interactive (-y / --noconfirm /
            # --non-interactive) and the user has already confirmed it above, so
            # there is nothing for a deadline to rescue anyone from.
            process = subprocess.run(command, timeout=system.PACKAGE_TRANSACTION_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            print(f" {FAIL} Package removal failed: {e}")
            return False
        if process.returncode == 0:
            print(f"\n {OK} Topo package removal completed.")
            for label in _remove_package_user_residue():
                print(f"  {OK} Removed {label}")
            print(
                f" {GRAY}If your shell still uses an old command path, run:{RESET} {BOLD}hash -r{RESET}"
            )
            return True
        print(f"\n {FAIL} Package removal failed with exit code {process.returncode}.")
        return False

    # 1. Identify files to remove
    to_remove: list[_RemoveItem] = []

    # The launcher link
    internal_dir = Path.home() / ".topo"
    for launcher_path in get_launcher_candidates():
        if (launcher_path.exists() or launcher_path.is_symlink()) and _launcher_points_to_topo(
            launcher_path, internal_dir
        ):
            to_remove.append(
                {"path": launcher_path, "desc": "Launcher script link", "type": "link"}
            )

    # Configuration directory
    config_dir = get_config_dir()
    if config_dir.exists() and not _config_dir_is_lock_only(config_dir):
        to_remove.append({"path": config_dir, "desc": "Configuration and whitelist", "type": "dir"})

    # Cache directory (if any)
    cache_dir = Path.home() / ".cache" / "topo"
    if cache_dir.exists():
        to_remove.append({"path": cache_dir, "desc": "Temporary scan cache", "type": "dir"})

    # Internal installation directory (from install.sh)
    if internal_dir.exists():
        to_remove.append({"path": internal_dir, "desc": "Main program files", "type": "dir"})

    # Deletion-audit / state directory (XDG_STATE_HOME/topo)
    state_dir = get_state_dir()
    if state_dir.exists():
        to_remove.append({"path": state_dir, "desc": "Deletion history / state", "type": "dir"})

    if not to_remove:
        print(f" {OK} No system integration found to remove.")
        return True

    # Calculate total size and prepare detailed list
    for item in to_remove:
        item["size"] = get_size_fast(item["path"])
    total_size: int = sum(item["size"] for item in to_remove)

    # 2. Preview (Compact Header)
    print(f"\n {PURPLE}{MARK_SECTION}{RESET} The following items will be removed:")
    for item in to_remove:
        size_str = bytes_to_human(int(item["size"]))
        disp_path = str(item["path"]).replace(str(Path.home()), "~")
        # `•` and not `{OK}`: nothing has been removed yet when this list prints,
        # so a ✓ per line claimed an outcome the preview cannot have. The two
        # `Dry run complete.` lines below keep their `{OK}` -- what completed there
        # is the preview itself.
        print(f"  {GRAY}• {disp_path:<40} ({item['desc']}, {size_str}){RESET}")

    if dry_run:
        print(f"\n {OK} {GRAY}Dry run complete. Total to free: {bytes_to_human(total_size)}{RESET}")
        print(f"  {GRAY}(Shell PATH entries added by topo would also be removed.){RESET}")
        return True

    # 3. Confirmation (Mole-style)
    if not _confirm_removal(
        f"\n {PURPLE}{MARK_PROMPT}{RESET} Remove topo ({bytes_to_human(total_size)}): "
        f"Press {GREEN}Enter{RESET} confirm, {GREEN}ESC{RESET} cancel: ",
        assume_yes,
    ):
        return False

    # 4. Execution
    print()
    had_errors = False
    for item in to_remove:
        p: Path = item["path"]
        ok, reason = safe_remove(
            p,
            use_trash=False,
            allow_app_data_removal=True,
            allow_self_removal=True,
        )
        if ok:
            print(f"  {OK} {GRAY}Removed {item['desc']}{RESET}")
        else:
            had_errors = True
            print(f"  {FAIL} Failed to remove {p}: {reason}")

    # Deletion auditing can recreate XDG_STATE_HOME/topo, while protection
    # checks can recreate ~/.config/topo to read an empty whitelist after the
    # original directory was removed. Clear both self-generated directories
    # once all uninstall work is finished -- they are the same two paths the
    # survey above already resolved, whether or not they were listed then.
    for generated_dir in (config_dir, state_dir):
        if not generated_dir.exists():
            continue
        try:
            shutil.rmtree(generated_dir)
        except OSError as e:
            had_errors = True
            print(f"  {FAIL} Failed to remove {generated_dir}: {e}")

    if _strip_topo_path_lines():
        print(f"  {OK} {GRAY}Removed PATH entry from shell config{RESET}")

    if had_errors:
        print(f"\n {WARN} {GRAY}Topo removal completed with errors (see above).{RESET}\n")
        return False

    print(f"\n {GREEN}✨ Topo has been successfully removed from your system!{RESET}\n")
    return True
