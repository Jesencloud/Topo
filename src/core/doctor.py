import json
import platform
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from .analyze import _get_core_binary
from .constants import BLUE, BOLD, CYAN, GRAY, GREEN, PURPLE, RED, RESET, YELLOW
from .install_source import get_install_root, get_install_source
from .system import get_invoking_user, get_os_id, run_command

DOCTOR_COMMAND_TIMEOUT = 5


def _check_tool(name: str, args: list[str] | None = None) -> tuple[bool, str]:
    if args is None:
        args = ["--version"]
    path = shutil.which(name)
    if not path:
        return False, "Not installed"
    res = run_command([name] + args, capture=True, timeout=DOCTOR_COMMAND_TIMEOUT)
    if res.ok:
        first_line = res.stdout.splitlines()[0] if res.stdout else "Installed"
        return True, first_line
    return True, "Installed (version check failed)"


def _read_topo_version(install_root: Path) -> str:
    try:
        version = (install_root / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "Unavailable (VERSION missing or unreadable)"
    return version or "Unavailable (VERSION empty)"


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


def run_doctor():
    print(f"\n{BOLD}{PURPLE}🩺 Topo Diagnostic Report{RESET}\n")

    # 1. System Environment
    print(f"{BOLD}{BLUE}System Environment{RESET}")
    print(f"  OS ID:         {CYAN}{get_os_id()}{RESET}")
    print(f"  Architecture:  {CYAN}{platform.machine()}{RESET}")
    print(f"  Python:        {CYAN}{platform.python_version()}{RESET} ({sys.executable})")
    print(f"  Invoking User: {CYAN}{get_invoking_user()}{RESET}")
    print()

    # 2. Topo Installation
    print(f"{BOLD}{BLUE}Topo Installation{RESET}")
    install_root = get_install_root()
    version = _read_topo_version(install_root)
    print(f"  Version:       {CYAN}{version}{RESET}")
    print(f"  Source:        {CYAN}{get_install_source()}{RESET}")
    print(f"  Install Root:  {CYAN}{install_root}{RESET}")
    print()

    # 3. Rust Engine
    print(f"{BOLD}{BLUE}Rust Engine{RESET}")
    engine = _get_core_binary()
    if engine and engine.exists():
        print(f"  {GREEN}✓{RESET} Executable: {CYAN}{engine}{RESET}")
        engine_ok, engine_detail = _check_rust_engine_response(engine)
        if engine_ok:
            print(f"  {GREEN}✓{RESET} Execution:  {GREEN}{engine_detail}{RESET}")
        else:
            print(f"  {RED}✗{RESET} Execution:  {RED}Failed{RESET} ({engine_detail})")
    else:
        print(f"  {RED}✗{RESET} Executable: {RED}Not found{RESET} at {engine}")
    print()

    # 4. Package Managers
    print(f"{BOLD}{BLUE}Package Managers & Tools{RESET}")
    for tool, args in [
        ("apt", ["--version"]),
        ("dpkg", ["--version"]),
        ("dnf", ["--version"]),
        ("rpm", ["--version"]),
        ("flatpak", ["--version"]),
        ("snap", ["--version"]),
    ]:
        ok, detail = _check_tool(tool, args)
        icon = f"{GREEN}✓{RESET}" if ok else f"{GRAY}-{RESET}"
        color = CYAN if ok else GRAY
        print(f"  {icon} {tool:<10} {color}{detail}{RESET}")
    print()

    # 5. File System & Trash
    print(f"{BOLD}{BLUE}File System Utilities{RESET}")
    for tool, args in [
        ("gio", ["version"]),
        ("trash-put", ["--version"]),
    ]:
        ok, detail = _check_tool(tool, args)
        icon = f"{GREEN}✓{RESET}" if ok else f"{GRAY}-{RESET}"
        color = CYAN if ok else GRAY
        print(f"  {icon} {tool:<10} {color}{detail}{RESET}")

    size_ok, size_detail = _check_rust_size_probe(engine)
    if size_ok is True:
        print(f"  {GREEN}✓{RESET} Rust Fast Size Calculation: {GREEN}{size_detail}{RESET}")
    elif size_ok is None:
        print(f"  {GRAY}-{RESET} Rust Fast Size Calculation: {GRAY}{size_detail}{RESET}")
    else:
        print(f"  {RED}✗{RESET} Rust Fast Size Calculation: {RED}Failed{RESET} ({size_detail})")
    print()

    # 6. Sudo Access
    print(f"{BOLD}{BLUE}Permissions{RESET}")
    has_sudo_session = run_command(
        ["sudo", "-n", "true"], capture=True, timeout=DOCTOR_COMMAND_TIMEOUT
    ).ok
    if has_sudo_session:
        print(f"  {GREEN}✓{RESET} Sudo Access: {GREEN}Active (Passwordless or Cached){RESET}")
    else:
        print(f"  {YELLOW}⚠{RESET} Sudo Access: {YELLOW}Requires Password prompt{RESET}")

    config_dir = Path.home() / ".config/topo"
    if config_dir.exists():
        print(f"  {GREEN}✓{RESET} Config Dir:  {CYAN}{config_dir}{RESET} (Exists)")
    else:
        print(f"  {GRAY}-{RESET} Config Dir:  {GRAY}{config_dir}{RESET} (Missing)")
    print()

    print(f"{BOLD}Diagnostic complete.{RESET}")
