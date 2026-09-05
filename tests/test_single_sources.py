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
    ICON_SLOT_WIDTH,
    SECONDS_PER_DAY,
    SECONDS_PER_HOUR,
    TOPO_VERSION,
    UNKNOWN_VERSION,
    AppType,
    read_topo_version,
)
from src.core.file_ops import get_deletion_log_path
from src.core.heavy_cache import PACKAGE_MANAGER_CACHE_DEFS
from src.core.install_source import get_install_root
from src.core.lock import LOCK_FILE_PATH
from src.core.package_manager import PACKAGE_MANAGERS
from src.core.paths import get_config_dir, get_state_dir
from src.core.system import PACKAGE_TRANSACTION_TIMEOUT
from src.core.text import icon_gap
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
    # And a VERSION file that does not decode. This runs at import time, so a
    # strict decode here did not break one command -- it broke every command,
    # before main() existed to catch anything. "replace" would be worse than
    # None: "1.2.�" is a wrong version rather than an absent one, and the
    # updater compares it against release tags.
    empty.write_bytes(b"1.2.\xff\n")
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


def _text_read_mode(call: ast.Call) -> str | None:
    """The mode string of a text read this call performs, or None if it is not one.

    ``os.open`` is not one: it returns a descriptor, decodes nothing, and its
    second positional argument is a flag mask rather than a mode string. Every
    other ``x.open()`` in src/ is ``Path.open``, whose mode is the first
    argument -- builtins' ``open`` puts it second.
    """
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr == "read_text":
            return "r"
        if func.attr != "open" or (isinstance(func.value, ast.Name) and func.value.id == "os"):
            return None
        mode_node = call.args[0] if call.args else None
    elif isinstance(func, ast.Name) and func.id == "open":
        mode_node = call.args[1] if len(call.args) > 1 else None
    else:
        return None
    for keyword in call.keywords:
        if keyword.arg == "mode":
            mode_node = keyword.value
    if mode_node is None:
        return "r"
    if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
        return mode_node.value
    # A computed mode cannot be classified, so it counts as a read: an unreadable
    # guard has to fail towards asking for the keyword, not towards silence.
    return "?"


def _strictly_decoding_file_reads() -> list[tuple[str, int]]:
    """Every read in src/ that would decode file bytes with the strict default."""
    offenders = []
    root = Path(__file__).parents[1] / "src"
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            mode = _text_read_mode(node)
            if mode is None or "b" in mode:
                continue
            # "w", "a" and "x" open a stream nothing is decoded out of. "r+" and
            # "w+" can be read from, so they stay in scope.
            if mode != "?" and not ({"r", "+"} & set(mode)):
                continue
            if not any(keyword.arg == "errors" for keyword in node.keywords):
                offenders.append((str(path.relative_to(root)), node.lineno))
    return offenders


def test_no_file_read_decodes_its_bytes_strictly():
    """Text reads in src/ name an error policy; the strict default is never used.

    Almost nothing topo reads is guaranteed UTF-8. Paths are arbitrary byte
    strings, /proc/swaps and /proc/net/dev quote them back, sysfs labels and
    /etc/os-release come from tables the board vendor wrote, and ~/.bashrc
    belongs to a user who may have carried it over from a pre-UTF-8 locale. A
    strict decode raises UnicodeDecodeError, which is a ValueError: the
    `except OSError` that every one of these reads is already wrapped in does
    not catch it, and main() only handles KeyboardInterrupt. That is how one
    GBK comment in a .bashrc used to crash `topo remove` *after* it had deleted
    the install directory, the whitelist and the deletion history.

    ``encoding=`` alone does not satisfy this and is not meant to: it names the
    codec, not what to do with a byte the codec rejects.

    Scope is reads only. A write encodes text topo itself produced, so there is
    nothing to be lenient about, and `errors=` on a write would hide a bug
    rather than a hostile byte. Which policy a read wants is still the call
    site's to make: "replace" where the text is only displayed or matched
    against ASCII, "ignore" where it is a tag file, and bytes-with-no-decode
    where the file is rewritten afterwards (remove.py's rc-file edit) -- those
    read_bytes() calls are outside this guard because they never decode at all.
    """
    assert _strictly_decoding_file_reads() == []


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


def _durations_written_as_literals() -> list[tuple[str, int, int]]:
    """Every 3600 or 86400 in src/ outside the module that names them."""
    root = Path(__file__).parents[1] / "src"
    defining_module = root / "core" / "constants.py"
    named = {SECONDS_PER_HOUR, SECONDS_PER_DAY}
    return [
        (str(path.relative_to(root)), node.lineno, node.value)
        for path in sorted(root.rglob("*.py"))
        if path != defining_module
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant)
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
        and node.value in named
    ]


def test_hours_and_days_are_spelled_with_their_constants_outside_the_definition():
    """SECONDS_PER_HOUR and SECONDS_PER_DAY are the names; nothing else may spell the numbers.

    SECONDS_PER_DAY was already here, and already used -- by exactly one caller.
    The other four sites wrote 86400 by hand: two age calculations in analyze.py
    and two rungs of navigator.py's "3h ago / 2d ago" ladder. That is the failure
    this guard covers: not a missing constant, but a constant that exists, is
    imported somewhere, and gets bypassed anyway, because a literal is one
    keystroke shorter than an import and nothing objects. SECONDS_PER_HOUR joined
    it rather than being added bare, for the same reason: 3600 had four call sites
    of its own -- the same ladder and status.py's uptime line -- and a new name
    with none of them converted would have been born already bypassed.

    Structural rather than behavioural, because there is no behaviour to test: a
    name and its number are the same value, so substituting the digits back passes
    every other test in the suite. Only the source can tell them apart.

    No exemption list: core/constants.py is out of scope because it is where the
    names are bound, and that is the one place the digits have to appear. Scoped to
    these two rather than every duration in the tree -- 60 is also a subprocess
    timeout in a dozen places, and navigator.py's month and year are 30 and 365
    days written as multiples of SECONDS_PER_DAY, which is what a reader needs and
    is nothing for this guard to check.
    """
    assert _durations_written_as_literals() == []
    assert SECONDS_PER_HOUR == 60 * 60
    assert SECONDS_PER_DAY == 24 * SECONDS_PER_HOUR


def _app_types_written_as_literals() -> list[tuple[str, int, str]]:
    """Every "Flatpak"/"Snap"/... string in src/ outside the module that names them."""
    root = Path(__file__).parents[1] / "src"
    defining_module = root / "core" / "constants.py"
    named = {app_type.value for app_type in AppType}
    return [
        (str(path.relative_to(root)), node.lineno, node.value)
        for path in sorted(root.rglob("*.py"))
        if path != defining_module
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in named
    ]


def test_app_types_are_spelled_with_the_enum_outside_its_definition():
    """AppType holds the eight words a scanned app can be labelled with; nothing else may.

    These had no definition at all: 31 bare strings, 22 of them in uninstall.py --
    eleven being the `app["type"] == "Flatpak"` comparisons the removal dispatches
    on -- five in a frozenset in the removal screen listing the types that need
    root, and four in the rows of the distro matrix. Nothing objected to a
    misspelling -- a package labelled "Flatpack" simply fell through to the
    "unsupported package type" branch at removal time -- and a ninth package
    manager meant finding all 31 from memory.

    Structural, like the guard above, because there is nothing behavioural to
    catch: AppType is a str subclass, so every one of those literals still compares
    equal to the member that replaced it, and putting the bare strings back passes
    the rest of the suite. Only the source can tell a definition from a copy.

    Scoped to whole-string equality, which is why the prose in package_manager.py
    that mentions "DNF" inside a sentence is not a hit, and why the lowercase
    manager keys ("apt", "dnf") are not either -- those answer a different question
    and have their own definition in the matrix.
    """
    assert _app_types_written_as_literals() == []
    # The four the distro matrix drives hold the members themselves, not equal
    # copies of the words: PackageManager.label is typed AppType precisely so the
    # row and the dispatch cannot drift into two spellings of one manager. This
    # asks for the type rather than the value because a plain "APT" would satisfy
    # every comparison and every set operation a str subclass takes part in.
    assert all(isinstance(manager.label, AppType) for manager in PACKAGE_MANAGERS)


# The two functions that answer "how many cells does this glyph take". A hand-made
# icon pad has to call one of them, because measuring is the whole point -- the
# hardcoded spacing they replaced is what got the columns wrong in the first place.
_WIDTH_MEASURES = {"display_width", "char_width"}


def _icon_widths_measured_by_hand() -> list[tuple[str, int]]:
    """Every place in src/ that measures an icon itself instead of calling icon_gap()."""
    root = Path(__file__).parents[1] / "src"
    defining_module = root / "core" / "text.py"
    found = []
    for path in sorted(root.rglob("*.py")):
        if path == defining_module:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or _called_name(node) not in _WIDTH_MEASURES:
                continue
            measured = set()
            for arg in [*node.args, *(kw.value for kw in node.keywords)]:
                # Identifiers and attributes, plus the string constants inside the
                # argument: a row icon usually arrives as a dict entry, and
                # `display_width(item["icon"])` names it only in the subscript.
                measured |= _string_constants(arg)
                measured |= {
                    inner.id if isinstance(inner, ast.Name) else inner.attr
                    for inner in ast.walk(arg)
                    if isinstance(inner, ast.Name | ast.Attribute)
                }
            if any("icon" in name.lower() for name in measured):
                found.append((str(path.relative_to(root)), node.lineno))
    return found


def test_the_icon_column_is_padded_in_one_place():
    """icon_gap() is the only thing that turns a row icon into spacing.

    status.py and ui/navigator.py each carried their own `_ICON_SLOT = 2` and their
    own `" " * max(0, _ICON_SLOT - display_width(icon))`, with a paragraph on both
    sides explaining the same fact: most row icons are East-Asian Wide, the ones
    carrying U+FE0F measure a single cell, so the column has to be padded by
    measurement rather than by hand. The two were equal over all 14 icons in use,
    which is what made it a copy rather than two rules.

    Structural rather than behavioural, like the two guards above: a second copy
    agrees with the first on the day it is written -- that is exactly why nothing
    objected the first time -- so only the source can tell there are two of them.

    Scoped to icons rather than to every pad. status.py's label field is measured
    the same way and is deliberately its own: it is sized from _ROW_LABELS, which
    is a fact about what that one report prints, not about how wide a glyph is.
    """
    assert _icon_widths_measured_by_hand() == []
    assert ICON_SLOT_WIDTH == 2
    assert icon_gap("\U0001f4ca") == " "  # two cells wide, so only the separator
    assert icon_gap("\U0001f5c2" + chr(0xFE0F)) == "  "  # narrow base + VS16, so a pad cell too
