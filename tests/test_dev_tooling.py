"""One pinned toolchain for CI and for the local gate.

ci.yml used to `pip install ruff mypy packaging vulture tach` unpinned, so the day
ruff added a rule or changed its formatter, unrelated pull requests went red and
"it passes locally" stopped meaning anything. The other half of the same problem:
`./check.sh` *formatted* while CI ran `--check`, so a run that printed a green
"Ruff format" had just made the change CI would have failed on, and the commit
that followed was clean only because the check itself had edited it.
"""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS = REPO_ROOT / "requirements-dev.txt"
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
CHECK_SCRIPT = REPO_ROOT / "check.sh"


def _pins() -> dict[str, str]:
    pins = {}
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        assert version, f"{line!r} is not pinned with =="
        pins[name] = version
    return pins


def test_every_dev_tool_is_pinned_to_an_exact_version():
    pins = _pins()

    assert set(pins) == {"mypy", "packaging", "pytest", "ruff", "tach", "vulture"}
    for name, version in pins.items():
        assert re.fullmatch(r"\d+(\.\d+)*", version), f"{name} is pinned to {version!r}"


def test_ci_installs_python_tools_only_from_the_pinned_file():
    workflow = CI_WORKFLOW.read_text()
    installs = re.findall(r"run: pip install (.+)", workflow)

    assert installs, "ci.yml no longer installs anything with pip"
    for arguments in installs:
        # -r for the lint job, -c for the test matrix (which deliberately does
        # not drag tach -- a compiled wheel -- onto the newest interpreter).
        assert re.match(r"-[rc] requirements-dev\.txt", arguments), arguments


def test_the_test_job_pins_the_packages_it_installs_by_name():
    # `-c` constrains without installing, so anything the test job needs has to
    # be named as well -- and named in requirements-dev.txt to be constrained.
    workflow = CI_WORKFLOW.read_text()
    constrained = re.search(r"run: pip install -c requirements-dev\.txt (.+)", workflow)
    assert constrained is not None

    for package in constrained.group(1).split():
        assert package in _pins()


def _check_script_commands(auto_fix: int) -> str:
    """The commands check.sh would run, without running any of them.

    Every tool invocation in check.sh's lint steps is built as a variable first,
    precisely so this can ask what the default mode does.
    """
    script = CHECK_SCRIPT.read_text()
    blocks = re.findall(r"^if \[ \$AUTO_FIX -eq 1 \]; then\n.*?^fi$", script, re.M | re.S)
    assert len(blocks) == 3, "check.sh no longer builds its lint commands in three AUTO_FIX blocks"

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f"AUTO_FIX={auto_fix}\n"
            + "\n".join(blocks)
            + '\nprintf "%s\\n" "$FORMAT_CMD" "$CARGO_FMT_CMD" "$RUFF_CMD" "$CLIPPY_CMD"\n',
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_check_script_asks_the_same_question_as_ci_by_default():
    commands = _check_script_commands(auto_fix=0)

    assert "ruff format --check src tests" in commands
    assert "cargo fmt --manifest-path topo-core/Cargo.toml --check" in commands
    assert "--fix" not in commands, "the default mode must not rewrite the working tree"


def test_check_script_rewrites_only_when_asked_to():
    commands = _check_script_commands(auto_fix=1)

    assert "ruff format src tests" in commands
    assert "--check" not in commands
    assert "ruff check --fix src tests" in commands


def test_check_script_points_at_the_pinned_file_when_a_tool_is_missing():
    # The skip hints are the only install instructions a contributor sees, so
    # they must not suggest an unpinned `pip install tach`.
    script = CHECK_SCRIPT.read_text()

    hints = re.findall(r"check SKIPPED \((.+?)\)", script)
    assert len(hints) == 2
    for hint in hints:
        assert hint == "pip install -r requirements-dev.txt"


def test_the_installer_does_not_leave_the_dev_requirements_in_the_install_tree():
    # install.sh's step 4 cleanup is a blacklist, so every new file at the repo
    # root has to be named there or it ships to ~/.topo.
    cleanup = re.search(r"^rm -rf \\\n(.*?)^$", (REPO_ROOT / "install.sh").read_text(), re.M | re.S)
    assert cleanup is not None

    assert "requirements-dev.txt" in cleanup.group(1)
