import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.core.config import clear_config_cache
from src.core.engine import reset_engine_state


@pytest.fixture(autouse=True)
def clean_engine_state():
    """Drop the process-wide "engine is unusable" latch around every test.

    One test that feeds the boundary a mismatched schema_version latches the
    engine off for the whole process, so without this every test that runs after
    it would silently get the pure-Python path. Same hazard, and same fix, as the
    config cache below.
    """
    reset_engine_state()
    yield
    reset_engine_state()


@pytest.fixture(autouse=True)
def clean_config_cache():
    """Drop the process-wide config cache around every test.

    ``get_config()`` memoizes, and ``get_config_dir()`` resolves ``Path.home()``,
    so one test that reads a setting would otherwise pin its temporary home's
    config for every test that runs after it -- including the ones that write a
    config file into a fresh ``test_env`` home and expect it to be honoured.
    """
    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture(autouse=True)
def no_real_sudo(monkeypatch):
    """Turn a test that reaches real sudo into a failure instead of a prompt.

    Nothing in the suite is meant to run a privileged command: the sudo-related
    tests all assert on the argv that ``run_command`` would build. A test that
    slips through and executes it does two things silently. sudo reads the
    password from /dev/tty rather than stdin, so pytest's capture does not stop
    it -- the run simply sits there until a human types something. And if one
    does, the command lands on the real machine, not on the temporary home.

    Which branch a cleaner takes depends on what is installed, so this has to be
    a runtime guard rather than a grep: a call that is unreachable here can be
    live on a machine that has flatpak, snap or docker.
    """
    real_run = subprocess.run

    def guarded(args, *rest, **kwargs):
        argv = list(args) if isinstance(args, (list, tuple)) else [args]
        if argv and Path(str(argv[0])).name == "sudo":
            raise AssertionError(
                f"test executed real sudo: {argv!r}\n"
                "Patch run_command (or the caller) instead -- this would ask the "
                "developer for a password and then act on their machine."
            )
        return real_run(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded)


@pytest.fixture
def test_env():
    """A temporary HOME, with the environment that resolves off it isolated too.

    HOME alone is not hermetic. get_state_dir() honours XDG_STATE_HOME, so a
    developer shell that exported it would send the deletion audit log, `history`
    and `remove`'s state directory to the real ~/.local/state rather than this
    temporary tree -- the test would read and write the developer's own state.
    So pin the XDG base dirs under the temp home as well: XDG_STATE_HOME is the
    one topo actually reads (get_config_dir deliberately ignores XDG_CONFIG_HOME),
    and the others are set defensively so a future reader cannot reintroduce the
    leak. Each is set to the same path the XDG default would resolve to under
    this HOME, so behaviour is identical to a clean shell -- the point is only to
    stop a dirty one leaking in.

    PATH is narrowed to the standard system directories for the same reason: a
    tool on a developer's personal PATH (~/.local/bin, a language version
    manager) must not change what an unmocked ``shutil.which`` finds.

    The patches ride a private MonkeyPatch, not the test's own ``monkeypatch``
    fixture: a test that calls ``monkeypatch.undo()`` mid-body (test_config does,
    to restore a patched json.dump) must not thereby revert HOME and read the
    developer's real config. This fixture's own undo runs in teardown regardless.
    """
    mp = pytest.MonkeyPatch()
    temp_home = Path(tempfile.mkdtemp(prefix="topo_test_home_"))
    mp.setenv("HOME", str(temp_home))
    mp.setenv("XDG_STATE_HOME", str(temp_home / ".local" / "state"))
    mp.setenv("XDG_CONFIG_HOME", str(temp_home / ".config"))
    mp.setenv("XDG_CACHE_HOME", str(temp_home / ".cache"))
    mp.setenv("XDG_DATA_HOME", str(temp_home / ".local" / "share"))
    mp.setenv("PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")

    # Pre-create some common structure
    (temp_home / ".config").mkdir(parents=True)
    (temp_home / ".cache").mkdir(parents=True)
    (temp_home / ".local" / "share").mkdir(parents=True)

    try:
        yield temp_home
    finally:
        mp.undo()
        shutil.rmtree(temp_home, ignore_errors=True)
