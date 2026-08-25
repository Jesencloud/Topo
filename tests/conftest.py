import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.core.config import clear_config_cache


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
    """Create a temporary home directory for testing to prevent accidental deletion."""
    old_home = os.environ.get("HOME")
    temp_home = tempfile.mkdtemp(prefix="topo_test_home_")
    os.environ["HOME"] = temp_home

    # Pre-create some common structure
    Path(temp_home).joinpath(".config").mkdir(parents=True)
    Path(temp_home).joinpath(".cache").mkdir(parents=True)
    Path(temp_home).joinpath(".local/share").mkdir(parents=True)

    yield Path(temp_home)

    # Cleanup
    shutil.rmtree(temp_home)
    if old_home:
        os.environ["HOME"] = old_home
    else:
        del os.environ["HOME"]
