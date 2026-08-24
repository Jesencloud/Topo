"""The conftest guard that keeps `pytest` from stopping to ask for a password."""

import subprocess

import pytest


def test_the_suite_cannot_execute_real_sudo():
    """Two clean tests used to reach `sudo flatpak uninstall --system` for real.

    sudo takes the password from /dev/tty, which pytest does not capture, so the
    run waited for a human instead of failing -- and once one was given, the
    command acted on the machine rather than on the temporary home.
    """
    with pytest.raises(AssertionError, match="real sudo"):
        subprocess.run(["sudo", "true"], check=False)


def test_the_guard_lets_unprivileged_commands_through():
    """It has to name only sudo: the suite really runs the Rust engine and pgrep."""
    assert subprocess.run(["true"], check=False).returncode == 0
