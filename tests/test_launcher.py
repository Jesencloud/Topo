"""The `topo` launcher: the Python floor, and what its one error message means.

Two failures used to look identical from the outside. On Debian 11 or RHEL 8 the
launcher happily imported a source tree written for 3.10+, and the resulting
SyntaxError/TypeError surfaced as "Could not find topo source modules in ..." --
a message about a directory that was perfectly intact. And any lazy import that
failed *inside* a feature module (`import termios` in remove.py, say) was caught
by the same handler and reported the same way.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "topo"
MISSING_SOURCE = "Could not find topo source modules"


def _run(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    # PYTHONDONTWRITEBYTECODE stripped rather than inherited: whether the launcher
    # writes a .pyc is one of the things under test here, and a developer with that
    # variable exported in their shell must not silently pass the half of it that
    # asserts bytecode *is* written.
    env = {key: value for key, value in os.environ.items() if key != "PYTHONDONTWRITEBYTECODE"}
    return subprocess.run(
        [sys.executable, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )


def _fake_install(tmp_path: Path, main_body: str | None) -> Path:
    """A copy of the launcher with a src/ tree we control, or none at all."""
    root = tmp_path / "install"
    root.mkdir()
    (root / "topo").write_text(LAUNCHER.read_text())
    if main_body is not None:
        (root / "src").mkdir()
        (root / "src/__init__.py").write_text("")
        (root / "src/main.py").write_text(main_body)
    return root


def test_the_launcher_refuses_an_interpreter_older_than_its_floor(tmp_path):
    # The guard has to fire before `from src.main import main`, so the only way
    # to reach it on this box is to lie about the version to the real
    # interpreter -- an old one is exactly what a test machine does not have.
    harness = tmp_path / "old_python.py"
    harness.write_text(
        "import runpy, sys\n"
        "sys.version_info = (3, 9, 2)\n"
        f"runpy.run_path({str(LAUNCHER)!r}, run_name='__main__')\n"
    )

    result = _run(str(harness))

    assert result.returncode == 1
    assert "Topo requires Python 3.10 or newer, but this is Python 3.9.2." in result.stderr
    # Not the installation message: the source tree is fine, the interpreter is not.
    assert MISSING_SOURCE not in result.stderr + result.stdout


def test_the_launcher_states_its_floor_in_a_dialect_the_floor_can_parse():
    # f-strings are 3.6, but a walrus or a match statement in this file would
    # make the guard itself a SyntaxError on the interpreters it exists to turn
    # away. compile() with the oldest feature version we claim to detect is the
    # closest thing to running one.
    source = LAUNCHER.read_text()
    guard = source.split("REAL_PATH")[0]

    compile(guard, "topo", "exec")
    assert "MIN_PYTHON = (3, 10)" in guard
    assert ".format(" in guard, "the guard must not depend on newer formatting syntax"


def test_a_missing_source_tree_is_the_one_thing_reported_as_a_broken_install(tmp_path):
    root = _fake_install(tmp_path, main_body=None)

    result = _run(str(root / "topo"), cwd=root)

    assert result.returncode == 1
    assert MISSING_SOURCE in result.stderr
    assert str(root) in result.stderr
    assert result.stdout == ""


def test_an_import_failing_inside_main_is_not_blamed_on_the_install(tmp_path):
    # What the old shape got wrong: main() sat in the same try as the import, so
    # a lazy `import termios` failing three modules deep pointed the user at a
    # directory that had nothing wrong with it.
    root = _fake_install(
        tmp_path,
        main_body="def main():\n    raise ImportError(\"No module named 'termios'\")\n",
    )

    result = _run(str(root / "topo"), cwd=root)

    assert result.returncode != 0
    assert MISSING_SOURCE not in result.stderr + result.stdout
    assert "No module named 'termios'" in result.stderr
    assert "Traceback" in result.stderr


def test_a_package_install_writes_no_bytecode_under_usr(tmp_path):
    """A package install must not grow files its own file list does not mention (D11).

    Packaging strips __pycache__ from the payload, but most of what topo does needs
    root: the first `sudo topo clean` used to have CPython write .pyc files all
    through /usr/lib/topo/src/, and dpkg deletes only what it recorded -- so
    `apt remove topo` left stale bytecode plus the empty directories it could not
    rmdir. Deciding this from the marker has to happen before the first import from
    src/, because that import is what writes the first .pyc.
    """
    root = _fake_install(tmp_path, main_body="def main():\n    pass\n")
    (root / ".topo-install-source").write_text("package\n")

    result = _run(str(root / "topo"), cwd=root)

    assert result.returncode == 0, result.stderr
    assert list(root.rglob("__pycache__")) == []
    assert list(root.rglob("*.pyc")) == []


def test_a_script_install_keeps_its_bytecode_cache(tmp_path):
    """The other side of D11: nothing was traded away for the git/script install.

    Its directory belongs to the user, the .pyc files there are removed with the
    tree, and paying to recompile every module on each launch would be a real cost
    to a TUI. An unreadable or unrecognised marker means script, exactly as
    get_install_source() reads it.
    """
    for index, marker in enumerate((None, "script\n", "nonsense\n")):
        home = tmp_path / f"case{index}"
        home.mkdir()
        root = _fake_install(home, main_body="def main():\n    pass\n")
        if marker is not None:
            (root / ".topo-install-source").write_text(marker)

        result = _run(str(root / "topo"), cwd=root)

        assert result.returncode == 0, result.stderr
        assert [p.name for p in root.rglob("__pycache__")] == ["__pycache__"]


def test_the_launcher_and_install_sh_agree_on_the_floor():
    # Two gates, one floor: install.sh refuses the same interpreter before it
    # writes anything, and the launcher refuses it again for the git and package
    # install paths that never run install.sh.
    launcher_floor = re.search(r"MIN_PYTHON = \((\d+), (\d+)\)", LAUNCHER.read_text())
    assert launcher_floor is not None
    install_floor = re.search(
        r"sys\.version_info >= \((\d+), (\d+)\)", (REPO_ROOT / "install.sh").read_text()
    )
    assert install_floor is not None

    assert launcher_floor.groups() == install_floor.groups() == ("3", "10")
