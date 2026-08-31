import json
import platform
import re
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from ..core.constants import (
    BLUE,
    BOLD,
    CYAN,
    FAIL,
    GRAY,
    GREEN,
    NA,
    OK,
    PURPLE,
    RED,
    RESET,
    WARN,
    YELLOW,
    read_topo_version,
)
from ..core.engine import get_core_binary
from ..core.install_source import get_install_root, get_install_source
from ..core.package_manager import PACKAGE_MANAGERS, detect_package_manager, resolve_admin_tool
from ..core.paths import get_config_dir
from ..core.system import C_LOCALE_ENV, get_invoking_user, get_os_id, run_command
from ..core.text import plural

DOCTOR_COMMAND_TIMEOUT = 5
VERSION_UNAVAILABLE = "Unavailable (VERSION missing, empty or unreadable)"
# A dotted number, plus a pre-release suffix when there is one (1.2.3-rc1), but
# not the sentence punctuation that may follow it.
_VERSION_TOKEN = re.compile(r"\d+(?:\.\d+)+(?:[-+~][\w.]+)?")
# Enough for any version, short enough to keep a row on one terminal line; only
# reached by a tool that prints no recognisable number at all.
VERSION_DETAIL_MAX_LENGTH = 40
# What `topo update` cannot do without: the download and the signature check.
# Warned about rather than failed on -- see run_doctor's docstring.
UPDATE_PREREQUISITES = (
    ("curl", "topo update cannot download a release"),
    ("gpg", "release signatures cannot be verified"),
)


def _short_version(output: str) -> str:
    """The version number alone, out of whatever `--version` printed.

    curl opens with its own version and then names every library it was linked
    against -- one ~300-character line that wrapped the report over several
    terminal rows. The tool's name is already this row's own column, so the row
    only needs the number: take the first version-looking token anywhere in the
    output (pacman's first line is ASCII art, so the first *line* is not enough),
    and fall back to a clipped first line for a tool that prints no number.
    """
    match = _VERSION_TOKEN.search(output)
    if match:
        return match.group(0)
    for line in output.splitlines():
        line = line.strip()
        if line:
            if len(line) > VERSION_DETAIL_MAX_LENGTH:
                return line[: VERSION_DETAIL_MAX_LENGTH - 3] + "..."
            return line
    return "Installed"


def _check_tool(name: str, args: list[str] | None = None) -> tuple[bool, str]:
    if args is None:
        args = ["--version"]
    if not shutil.which(name):
        return False, "Not installed"
    # C locale because the answer is parsed, not displayed: dnf5 and rpm print
    # "版本" where a translation exists, and _short_version's fallback line would
    # otherwise be a sentence in whatever language the box happens to be set to.
    res = run_command([name] + args, capture=True, timeout=DOCTOR_COMMAND_TIMEOUT, env=C_LOCALE_ENV)
    if res.ok:
        return True, _short_version(res.stdout)
    return True, "Installed (version check failed)"


def _print_tool_row(tool: str, args: list[str] | None = None, missing_note: str = "") -> None:
    """One report row: ✓ and the version when installed, otherwise a grey dash.

    A tool with a missing_note is a soft requirement: absent, it gets a ⚠ and the
    consequence spelled out, never an entry in `failures`.
    """
    ok, detail = _check_tool(tool, args)
    if ok:
        print(f"  {OK} {tool:<10} {CYAN}{detail}{RESET}")
    elif missing_note:
        print(f"  {WARN} {tool:<10} {YELLOW}{detail} -- {missing_note}{RESET}")
    else:
        print(f"  {NA} {tool:<10} {GRAY}{detail}{RESET}")


def _command_failure_detail(result) -> str:
    if result.timed_out:
        return f"Timed out after {DOCTOR_COMMAND_TIMEOUT}s"
    detail = (result.stderr or result.stdout or result.error).strip()
    if detail:
        return detail.splitlines()[0]
    return f"Exit {result.returncode}"


def _check_rust_engine_response(engine: Path) -> tuple[bool, str]:
    result = run_command([str(engine)], capture=True, timeout=DOCTOR_COMMAND_TIMEOUT)
    if "Usage:" in result.stdout or "Usage:" in result.stderr:
        return True, "OK (Engine responded)"
    return False, _command_failure_detail(result)


def _check_rust_size_probe(engine: Path | None) -> tuple[bool | None, str]:
    if not engine or not engine.exists():
        return None, "Skipped (Engine missing)"

    try:
        with TemporaryDirectory(prefix="topo-doctor-") as temp_dir:
            probe_dir = Path(temp_dir)
            sample_file = probe_dir / "sample.txt"
            sample_file.write_text("topo\n", encoding="utf-8")

            result = run_command(
                [str(engine), str(probe_dir)],
                capture=True,
                timeout=DOCTOR_COMMAND_TIMEOUT,
            )
            if not result.ok:
                return False, _command_failure_detail(result)

            try:
                data = json.loads(result.stdout)
            except json.JSONDecodeError:
                return False, "Invalid engine JSON output"

            size_bytes = int(data.get("total_size_bytes", -1))
            if size_bytes < sample_file.stat().st_size:
                return False, "Invalid size result"
    except (OSError, TypeError, ValueError) as e:
        return False, str(e)

    return True, "OK"


def _report_system_environment() -> None:
    print(f"{BOLD}{BLUE}System Environment{RESET}")
    print(f"  OS ID:         {CYAN}{get_os_id()}{RESET}")
    print(f"  Architecture:  {CYAN}{platform.machine()}{RESET}")
    print(f"  Python:        {CYAN}{platform.python_version()}{RESET} ({sys.executable})")
    print(f"  Invoking User: {CYAN}{get_invoking_user()}{RESET}")
    print()


def _report_topo_installation() -> list[str]:
    print(f"{BOLD}{BLUE}Topo Installation{RESET}")
    failures: list[str] = []
    install_root = get_install_root()
    # Read from the tree this report calls the install root, through the same
    # reader core.constants uses, so doctor and `topo --version` cannot disagree.
    version = read_topo_version(install_root / "VERSION")
    if version is None:
        failures.append("VERSION unreadable")
    print(f"  Version:       {CYAN}{version or VERSION_UNAVAILABLE}{RESET}")
    print(f"  Source:        {CYAN}{get_install_source()}{RESET}")
    print(f"  Install Root:  {CYAN}{install_root}{RESET}")
    print()
    return failures


def _report_rust_engine(engine: Path | None) -> list[str]:
    print(f"{BOLD}{BLUE}Rust Engine{RESET}")
    failures: list[str] = []
    if engine and engine.exists():
        print(f"  {OK} Executable: {CYAN}{engine}{RESET}")
        engine_ok, engine_detail = _check_rust_engine_response(engine)
        if engine_ok:
            print(f"  {OK} Execution:  {GREEN}{engine_detail}{RESET}")
        else:
            failures.append("Rust engine does not run")
            print(f"  {FAIL} Execution:  {RED}Failed{RESET} ({engine_detail})")
    else:
        failures.append("Rust engine missing")
        print(f"  {FAIL} Executable: {RED}Not found{RESET} at {engine}")
    print()
    return failures


def _report_update_prerequisites() -> None:
    print(f"{BOLD}{BLUE}Update Prerequisites{RESET}")
    for tool, consequence in UPDATE_PREREQUISITES:
        _print_tool_row(tool, missing_note=consequence)
    print()


def _report_package_managers() -> None:
    print(f"{BOLD}{BLUE}Package Managers & Tools{RESET}")
    manager = detect_package_manager()
    if manager is None:
        supported = ", ".join(known.label for known in PACKAGE_MANAGERS)
        print(f"  {NA} No supported package manager detected ({supported})")
    else:
        print(f"  {OK} {'Detected':<10} {CYAN}{manager.label}{RESET}")
        # The binaries topo actually runs, not the family's front-end names: it
        # used to probe `apt` and `dpkg` while running apt-get and dpkg-query, and
        # `dnf` on a Fedora where dnf is only a compat symlink to dnf5.
        for tool in dict.fromkeys([resolve_admin_tool(manager), manager.query_tool]):
            _print_tool_row(tool)
    # Cross-distro app sources, scanned by `topo uninstall` whatever the distro is.
    for tool in ("flatpak", "snap"):
        _print_tool_row(tool)
    print()


def _report_filesystem_utilities(engine: Path | None) -> list[str]:
    print(f"{BOLD}{BLUE}File System Utilities{RESET}")
    failures: list[str] = []
    _print_tool_row("gio", ["version"])
    _print_tool_row("trash-put")

    size_ok, size_detail = _check_rust_size_probe(engine)
    if size_ok is True:
        print(f"  {OK} Rust Fast Size Calculation: {GREEN}{size_detail}{RESET}")
    elif size_ok is None:
        print(f"  {NA} Rust Fast Size Calculation: {GRAY}{size_detail}{RESET}")
    else:
        failures.append("Rust size probe failed")
        print(f"  {FAIL} Rust Fast Size Calculation: {RED}Failed{RESET} ({size_detail})")
    print()
    return failures


def _report_permissions() -> None:
    print(f"{BOLD}{BLUE}Permissions{RESET}")
    has_sudo_session = run_command(
        ["sudo", "-n", "true"], capture=True, timeout=DOCTOR_COMMAND_TIMEOUT
    ).ok
    if has_sudo_session:
        print(f"  {OK} Sudo Access: {GREEN}Active (Passwordless or Cached){RESET}")
    else:
        print(f"  {WARN} Sudo Access: {YELLOW}Requires Password prompt{RESET}")

    config_dir = get_config_dir()
    if config_dir.exists():
        print(f"  {OK} Config Dir:  {CYAN}{config_dir}{RESET} (Exists)")
    else:
        print(f"  {NA} Config Dir:  {GRAY}{config_dir}{RESET} (Missing)")
    print()


def run_doctor() -> bool:
    """Print the diagnostic report; False when a hard problem was found.

    "Hard" is deliberately narrow. Only what breaks topo itself counts: an
    unreadable VERSION (broken install tree), a missing or non-responding Rust
    engine, and a failing size probe. Optional tooling is not a failure --
    `apt-get` is *supposed* to be absent on Fedora, `trash-put` on a headless
    box, and a sudo password prompt is the normal case. A doctor that exited
    non-zero for those would be as useless to a script as one that always
    exited 0.

    curl and gpg are the one grey area: without them `topo update` cannot work,
    but everything else can, and a package install never updates itself. They
    get a ⚠ with the consequence spelled out, not a failure.

    The report is the sections below in this order, each printing its own
    heading; the three that can find a hard problem hand their failures back,
    and this function owns the verdict. Those headings used to be numbered
    comments above blocks of the same body, one per `print()` that said the same
    words -- `# 5. Package Managers` over a heading reading "Package Managers &
    Tools", which is the version that had already drifted.
    """
    print(f"\n{BOLD}{PURPLE}🩺 Topo Diagnostic Report{RESET}\n")

    _report_system_environment()
    failures = _report_topo_installation()
    # Resolved once, here, because two sections need it: the engine's own section
    # and the size probe under File System Utilities. Asking twice would let one
    # section report an engine the other did not use.
    engine = get_core_binary()
    failures += _report_rust_engine(engine)
    _report_update_prerequisites()
    _report_package_managers()
    failures += _report_filesystem_utilities(engine)
    _report_permissions()

    if failures:
        print(f"{BOLD}{RED}Diagnostic complete: {plural(len(failures), 'problem')} found.{RESET}")
        for failure in failures:
            print(f"  {FAIL} {failure}")
        return False

    print(f"{BOLD}Diagnostic complete.{RESET}")
    return True
