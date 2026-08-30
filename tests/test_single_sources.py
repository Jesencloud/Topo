"""The things topo must not have more than one answer for.

All of them used to be copied around: the VERSION file was read by three
components with three different fallbacks, ~/.config/topo was spelled out by
hand in six places next to the get_config_dir() that exists for it, and the XDG
state directory was derived three times -- twice by the command that deletes it.
"""

import ast
from pathlib import Path

from src.clean import system as clean_system
from src.core import constants
from src.core.config import get_config_file
from src.core.constants import (
    DETECTED_APPS_FILE,
    TOPO_VERSION,
    UNKNOWN_VERSION,
    read_topo_version,
)
from src.core.file_ops import get_deletion_log_path
from src.core.heavy_cache import PACKAGE_MANAGER_CACHE_DEFS
from src.core.install_source import get_install_root
from src.core.lock import LOCK_FILE_PATH
from src.core.package_manager import PACKAGE_MANAGERS
from src.core.paths import get_config_dir, get_state_dir
from src.core.system import PACKAGE_TRANSACTION_TIMEOUT
from src.core.whitelist import get_whitelist_file
from src.manage.update import _parse_version
from src.ui.screens.uninstall import NEEDS_SUDO_TYPES


def test_read_topo_version_strips_the_stored_value(tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("  1.2.3\n")

    assert read_topo_version(version_file) == "1.2.3"


def test_read_topo_version_reports_anything_unusable_as_none(tmp_path):
    # One definition of "cannot be read", shared by the three callers that used
    # to invent their own: missing, empty and whitespace-only all mean the same.
    assert read_topo_version(tmp_path / "absent") is None
    empty = tmp_path / "VERSION"
    empty.write_text("")
    assert read_topo_version(empty) is None
    empty.write_text("\n \n")
    assert read_topo_version(empty) is None


def test_unknown_version_cannot_be_mistaken_for_a_version():
    # The updater compares TOPO_VERSION against the latest release tag, so the
    # fallback must fail to parse: it used to be 0.0.0, which made every remote
    # tag look newer and turned a lost VERSION file into an unasked reinstall.
    assert _parse_version(UNKNOWN_VERSION) is None


def test_topo_version_comes_from_the_version_file():
    assert (read_topo_version() or UNKNOWN_VERSION) == TOPO_VERSION
    assert constants.VERSION_FILE.name == "VERSION"


def test_doctor_reads_the_same_version_file_as_the_banner():
    # doctor reports on the tree at get_install_root(); constants resolves its
    # own __file__. Both have to name the same file, or `topo doctor` and
    # `topo --version` could still disagree -- which is the whole point of
    # having one reader.
    assert get_install_root() / "VERSION" == constants.VERSION_FILE


def test_every_topo_config_path_hangs_off_get_config_dir():
    config_dir = get_config_dir()

    assert config_dir / "topo.lock" == LOCK_FILE_PATH
    assert config_dir / "detected_apps.json" == DETECTED_APPS_FILE
    assert get_config_file() == config_dir / "config.json"
    assert get_whitelist_file() == config_dir / "whitelist.json"


def test_the_audit_log_and_topo_remove_agree_on_the_state_dir(monkeypatch):
    # `topo remove` deletes this directory and the deletion log creates it, from
    # what used to be three separate XDG_STATE_HOME derivations. If they drift,
    # removal silently leaves the history behind.
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/topo-state-probe")

    assert get_state_dir() == Path("/tmp/topo-state-probe/topo")
    assert get_deletion_log_path() == get_state_dir() / "deletions.log"

    monkeypatch.delenv("XDG_STATE_HOME")
    assert get_state_dir() == Path.home() / ".local/state/topo"


def test_every_layer_knows_every_package_manager_in_the_matrix(monkeypatch):
    """Adding a row to the matrix must not leave a layer behind.

    Each of these used to be a hand-kept copy, and each had already drifted:
    Analyze had no zypper row, so `topo clean` never touched the package cache on
    openSUSE, and the removal screen decided which app types need root from its
    own frozenset.
    """
    keys = {manager.key for manager in PACKAGE_MANAGERS}
    labels = {manager.label for manager in PACKAGE_MANAGERS}

    assert {definition.key for definition in PACKAGE_MANAGER_CACHE_DEFS} == keys
    # Removing a distro package is always a privileged operation, so the UI must
    # already know to ask for a password for every manager topo can drive.
    assert labels <= NEEDS_SUDO_TYPES

    monkeypatch.setattr(clean_system.Path, "exists", lambda self: True)
    for key in keys:
        assert clean_system._get_package_manager_cache_paths(key), key


def _hand_rolled_json_file_io() -> list[tuple[str, int]]:
    """Every ``json.dump``/``json.load`` in src/ outside json_store.py.

    ``json.loads`` on a string is not in scope -- parsing a subprocess's stdout is
    a different job. This looks only for the two calls that take a file object,
    which is where the truncate-then-write and the strict-decode live.
    """
    calls = []
    root = Path(__file__).parents[1] / "src"
    for path in sorted(root.rglob("*.py")):
        if path.name == "json_store.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"dump", "load"}:
                continue
            if isinstance(node.func.value, ast.Name) and node.func.value.id == "json":
                calls.append((str(path.relative_to(root)), node.lineno))
    return calls


def test_topo_json_state_is_read_and_written_in_one_place():
    """One reader and one writer for the config, the whitelist and the registry.

    All three used to open and dump by hand, and all three had the same two bugs.
    ``open(path, "w")`` truncates before the replacement is written, so an
    interrupted dump leaves an empty file where the old one was -- for the
    whitelist, a lost protection rather than a lost setting. And ``json.load`` on
    a file whose bytes are not UTF-8 raises UnicodeDecodeError, a ValueError that
    every one of their ``except (OSError, JSONDecodeError)`` clauses missed.

    Structural, like the subprocess guard above, because the failure mode is the
    obvious way to write it: the next state file added by hand would arrive with
    both defects again.
    """
    assert _hand_rolled_json_file_io() == []


def _capturing_text_subprocess_calls() -> list[ast.Call]:
    """Every text-mode subprocess call in src/ that reads the child's output."""
    calls = []
    root = Path(__file__).parents[1] / "src"
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in {"run", "check_output", "Popen"}:
                continue
            kwargs = {kw.arg for kw in node.keywords}
            if not kwargs & {"text", "universal_newlines", "encoding"}:
                continue
            # check_output always captures; the others only when asked to. A
            # `stdout=DEVNULL` counts as asked-for here, which is fine: the pair
            # of them is what a capturing call looks like, and being generous
            # costs an unnecessary errors= at worst.
            captures = node.func.attr == "check_output" or bool(
                kwargs & {"capture_output", "stdout", "stderr"}
            )
            if captures and "errors" not in kwargs:
                calls.append((str(path.relative_to(root)), node.lineno))
    return calls


def test_no_subprocess_call_decodes_child_output_strictly():
    """One decoding policy for captured output: never the strict default.

    A filename is an arbitrary byte string on Linux and a proxy can answer in any
    encoding, so any command whose output topo reads can hand back bytes that are
    not UTF-8. Strict decoding raises UnicodeDecodeError inside subprocess itself
    -- a ValueError, so neither `except OSError` nor `except SubprocessError`
    stops it, and main() only catches KeyboardInterrupt. That is how one Latin-1
    filename used to turn any topo command into a raw traceback, and how a
    captive portal's error page used to crash `topo update`.

    This is a structural guard rather than six separate cases because the failure
    mode is a missing keyword: the next call site added without it would
    reintroduce the same crash somewhere new.
    """
    assert _capturing_text_subprocess_calls() == []


# The tools whose transaction database a SIGKILL can leave needing manual repair.
# npm and multipass are deliberately not here: a killed `npm uninstall -g` leaves
# files behind, not a package manager that refuses to run until a human runs
# `dpkg --configure -a`.
_PACKAGE_TOOLS = {
    "apt",
    "apt-get",
    "dpkg",
    "dnf",
    "dnf5",
    "yum",
    "rpm",
    "pacman",
    "zypper",
    "snap",
    "flatpak",
}
_DESTRUCTIVE_SUBCOMMANDS = {
    "purge",
    "remove",
    "autoremove",
    "uninstall",
    "erase",
    "-R",
    "-Rs",
    "-Rns",
}
# Helpers that hand back a whole package-manager argv. The tokens live in
# core/package_manager.py's dataclasses, a module away from any call, so nothing
# in the calling function spells "remove" -- the name of the builder is the only
# thing there is to match on.
_PACKAGE_ARGV_BUILDERS = {"get_package_remove_argv", "get_package_upgrade_argv"}
# A resolver run is not a transaction: nothing is half-removed when it is killed,
# so these keep the plain default. The list is longer than "--dry-run" because
# uninstall.py's collateral preview asks each tool in its own dialect -- apt-get
# purge -s, pacman -Rns --print-format, dnf repoquery, rpm -q --whatrequires --
# and four of those spell a removal subcommand on the way to asking a question.
_PREVIEW_MARKERS = {
    "--dry-run",
    "--simulate",
    "--assume-no",
    "--print-format",
    "repoquery",
    "--whatrequires",
}


def _string_constants(node: ast.AST) -> set[str]:
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _called_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _package_transactions_without_the_shared_timeout() -> list[tuple[str, int]]:
    """Every destructive package transaction in src/ that keeps a deadline."""
    offenders = []
    root = Path(__file__).parents[1] / "src"
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for scope in ast.walk(tree):
            if not isinstance(scope, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            # argv is sometimes built up under a name first -- appended to (the
            # flatpak branch of uninstall.py adds its scope flag) or handed over
            # whole by one of the builders -- so the assignments in the same
            # function have to be read alongside the call itself.
            built: dict[str, set[str]] = {}
            from_builder: set[str] = set()
            for node in ast.walk(scope):
                targets = []
                if isinstance(node, ast.Assign):
                    targets = [t for t in node.targets if isinstance(t, ast.Name)]
                elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                    targets = [node.target]
                for target in targets:
                    built.setdefault(target.id, set()).update(_string_constants(node.value))
                    for call in ast.walk(node.value):
                        if (
                            isinstance(call, ast.Call)
                            and _called_name(call) in _PACKAGE_ARGV_BUILDERS
                        ):
                            from_builder.add(target.id)
            for node in ast.walk(scope):
                if not isinstance(node, ast.Call) or not node.args:
                    continue
                called = _called_name(node)
                # subprocess.run as well as run_command: topo's own removal and
                # upgrade go straight to subprocess, and they are the two most
                # consequential package transactions in the tree.
                if called not in {"run_command", "run"}:
                    continue
                argv = node.args[0]
                tokens = _string_constants(argv)
                if isinstance(argv, ast.Name):
                    tokens |= built.get(argv.id, set())
                handed_over = isinstance(argv, ast.Name) and argv.id in from_builder
                if not handed_over:
                    if not tokens & _DESTRUCTIVE_SUBCOMMANDS or tokens & _PREVIEW_MARKERS:
                        continue
                    # apt-get's -s is the one preview flag short enough to collide
                    # with something else, so it only counts when apt is the tool.
                    if "-s" in tokens and tokens & {"apt", "apt-get"}:
                        continue
                kwargs = {kw.arg: kw.value for kw in node.keywords}
                # Three ways to recognise a package manager, because none of them
                # is present everywhere: the tool named in the argv (uninstall.py
                # spells it out), a use_sudo keyword (what every privileged branch
                # in clean/ has, and those lead with a resolved `tool` variable
                # rather than a literal), or an argv assembled under a name above
                # the call. The last two are deliberately loose -- over-including a
                # call costs one explicit keyword, under-including it costs a
                # half-finished transaction.
                if not (
                    handed_over
                    or tokens & _PACKAGE_TOOLS
                    or "use_sudo" in kwargs
                    or not isinstance(argv, ast.List)
                ):
                    continue
                timeout = kwargs.get("timeout")
                if timeout is None or not ast.unparse(timeout).endswith(
                    "PACKAGE_TRANSACTION_TIMEOUT"
                ):
                    offenders.append((str(path.relative_to(root)), node.lineno))
    return offenders


def test_no_package_transaction_runs_on_a_deadline():
    """One timeout policy for the package transactions: none of them may have one.

    subprocess.run SIGKILLs the child when a timeout expires, and with capture=True
    sudo execs the tool instead of forking a monitor, so the process killed is dpkg
    or rpm itself, mid-transaction. Purging a kernel runs update-initramfs and
    update-grub from a maintainer script, minutes per kernel on an encrypted root,
    and the 300-second default used to cover ten such calls -- the kernel loop
    would then walk into the next purge with dpkg already half-configured.

    Both shapes are in scope. The two most consequential transactions in the tree
    are not run_command calls at all: `topo remove` and `topo update` hand a
    get_package_*_argv() list straight to subprocess.run, and those two had kept a
    literal timeout=300 while every removal around them was being freed of one.

    Structural, like the two guards above, because the failure mode is an omission:
    the default applies to every call that says nothing, so the next removal added
    without the keyword is back on a deadline, and nothing about it looks wrong.
    """
    assert _package_transactions_without_the_shared_timeout() == []
    # None rather than a generous number: a finite deadline only moves the cliff,
    # and every call site above is non-interactive by construction, so there is
    # nothing for a deadline to rescue the user from.
    assert PACKAGE_TRANSACTION_TIMEOUT is None
