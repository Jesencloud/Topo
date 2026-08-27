from pathlib import Path
from unittest.mock import patch

import pytest

from src.core.package_manager import APT
from src.core.system import CommandResult
from src.manage import doctor


def _command_result(args, returncode=0, stdout="", stderr="", error="", timed_out=False):
    return CommandResult(
        args=list(args),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        error=error,
        timed_out=timed_out,
    )


def test_run_doctor_continues_when_version_file_is_missing(tmp_path, capsys):
    install_root = tmp_path / "install"
    install_root.mkdir()
    home = tmp_path / "home"
    home.mkdir()

    with (
        patch("src.manage.doctor.get_install_root", return_value=install_root),
        patch("src.manage.doctor.get_install_source", return_value="script"),
        patch("src.manage.doctor.get_core_binary", return_value=None),
        patch("src.manage.doctor.shutil.which", return_value=None),
        patch("src.manage.doctor.Path.home", return_value=home),
        patch(
            "src.manage.doctor.run_command",
            return_value=_command_result(["sudo"], returncode=1),
        ),
    ):
        assert doctor.run_doctor() is False

    output = capsys.readouterr().out
    assert doctor.VERSION_UNAVAILABLE in output
    # An unreadable VERSION and a missing engine are both hard failures: the
    # install tree is broken. The report still prints in full -- doctor's job is
    # to describe the environment, not to bail on the first problem -- but the
    # exit code has to say something went wrong.
    assert "Diagnostic complete: 2 problems found." in output
    assert "✗ VERSION unreadable" in output
    assert "✗ Rust engine missing" in output


def test_run_doctor_uses_temporary_size_probe_with_short_timeout(tmp_path):
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "VERSION").write_text("1.2.3\n")
    home = tmp_path / "home"
    (home / ".config" / "topo").mkdir(parents=True)
    engine = tmp_path / "topo-core-x86_64"
    engine.write_text("#!/bin/sh\n")
    engine.chmod(0o755)
    engine_calls = []

    def fake_run_command(args, capture=True, timeout=300):
        if args and args[0] == str(engine):
            engine_calls.append((args, capture, timeout))
            if len(args) == 1:
                return _command_result(args, returncode=1, stderr="Usage: topo-core <path>")

            probe_dir = Path(args[1])
            assert probe_dir != home
            assert home not in probe_dir.parents
            assert (probe_dir / "sample.txt").exists()
            return _command_result(args, stdout='{"total_size_bytes": 5}')

        return _command_result(args, returncode=1)

    with (
        patch("src.manage.doctor.get_install_root", return_value=install_root),
        patch("src.manage.doctor.get_install_source", return_value="script"),
        patch("src.manage.doctor.get_core_binary", return_value=engine),
        patch("src.manage.doctor.shutil.which", return_value=None),
        patch("src.manage.doctor.Path.home", return_value=home),
        patch("src.manage.doctor.run_command", side_effect=fake_run_command),
    ):
        assert doctor.run_doctor() is True

    assert len(engine_calls) == 2
    assert all(
        timeout == doctor.DOCTOR_COMMAND_TIMEOUT for _args, _capture, timeout in engine_calls
    )


def _report_with_no_tools_installed(tmp_path, manager):
    """run_doctor() on a broken-engine box where nothing optional is installed."""
    install_root = tmp_path / "install"
    install_root.mkdir()
    (install_root / "VERSION").write_text("1.2.3\n")
    home = tmp_path / "home"
    home.mkdir()

    with (
        patch("src.manage.doctor.get_install_root", return_value=install_root),
        patch("src.manage.doctor.get_install_source", return_value="script"),
        patch("src.manage.doctor.get_core_binary", return_value=None),
        patch("src.manage.doctor.detect_package_manager", return_value=manager),
        patch("src.manage.doctor.shutil.which", return_value=None),
        patch("src.manage.doctor.Path.home", return_value=home),
        patch(
            "src.manage.doctor.run_command",
            return_value=_command_result(["sudo"], returncode=1),
        ),
    ):
        return doctor.run_doctor()


def _tool_column(output):
    """The tool name of every report row, whatever colour it was printed in."""
    return {parts[1] for parts in (line.split() for line in output.splitlines()) if len(parts) > 1}


def test_doctor_probes_the_binaries_topo_actually_runs(tmp_path, capsys):
    """The probe list comes from the matrix row, not from a hand-kept list.

    doctor used to ask about `apt` and `dpkg` while topo runs apt-get and
    dpkg-query, about `dnf` on a Fedora where dnf is only a compat symlink to
    dnf5, and about pacman not at all.
    """
    assert _report_with_no_tools_installed(tmp_path, APT) is False

    output = capsys.readouterr().out
    tools = _tool_column(output)
    assert {"apt-get", "dpkg-query", "flatpak", "snap"} <= tools
    assert not tools & {"apt", "dpkg", "dnf", "rpm"}
    assert APT.label in output
    # Only the engine is a hard failure -- an absent curl, gpg, apt-get or
    # dpkg-query is a machine's normal state, not a broken install.
    assert "Diagnostic complete: 1 problem found." in output


def test_doctor_warns_about_update_prerequisites_without_failing(tmp_path, capsys):
    assert _report_with_no_tools_installed(tmp_path, None) is False

    output = capsys.readouterr().out
    assert "⚠ curl" in output
    assert "topo update cannot download a release" in output
    assert "⚠ gpg" in output
    assert "release signatures cannot be verified" in output
    # Nothing in the matrix claimed this machine, and the report says so instead
    # of silently listing no package tools at all.
    assert "No supported package manager detected" in output
    assert "Diagnostic complete: 1 problem found." in output


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        # The row this was written for: curl names every library it links against.
        (
            (
                "curl 8.18.0 (x86_64-redhat-linux-gnu) libcurl/8.18.0 OpenSSL/3.5.7 "
                "zlib/1.3.1.zlib-ng brotli/1.2.0 libidn2/2.3.8 nghttp2/1.68.0\n"
                "Release-Date: 2026-01-14\n"
            ),
            "8.18.0",
        ),
        ("gpg (GnuPG) 2.4.9\nlibgcrypt 1.11.0\n", "2.4.9"),
        ("2.88.3\n", "2.88.3"),  # gio version prints the number and nothing else
        ("Flatpak 1.18.1\n", "1.18.1"),
        ("dnf5 version 5.4.3.0\n", "5.4.3.0"),
        # dpkg-query pads its own line out with a sentence, and ends it with a dot
        # that is punctuation rather than part of the version.
        ("Debian dpkg-query package management program version 1.23.7 (amd64).\n", "1.23.7"),
        # pacman opens with ASCII art, so the first line carries no version at all.
        ("\n .--.                  Pacman v6.0.2 - libalpm v13.0.2\n", "6.0.2"),
        ("trash-put 0.24.5.26\n", "0.24.5.26"),
        ("topo 1.2.3-rc1\n", "1.2.3-rc1"),
    ],
)
def test_a_tool_row_shows_the_version_and_not_the_link_line(output, expected):
    assert doctor._short_version(output) == expected


def test_a_tool_that_prints_no_version_still_gets_a_row_that_fits():
    assert doctor._short_version("") == "Installed"
    assert doctor._short_version("   \n\n") == "Installed"

    detail = doctor._short_version("built from git, no version number here at all\n")

    assert len(detail) == doctor.VERSION_DETAIL_MAX_LENGTH
    assert detail.endswith("...")


def test_the_version_probe_asks_in_the_c_locale():
    # The output is parsed now, so it has to be asked for in a language this code
    # knows: dnf5 and rpm both print a translated "version" line otherwise.
    calls = []

    def fake_run_command(args, capture=True, timeout=300, env=None):
        calls.append((args, env))
        return _command_result(args, stdout="curl 8.18.0 (x86_64) libcurl/8.18.0\n")

    with (
        patch("src.manage.doctor.shutil.which", return_value="/usr/bin/curl"),
        patch("src.manage.doctor.run_command", side_effect=fake_run_command),
    ):
        assert doctor._check_tool("curl") == (True, "8.18.0")

    assert calls == [(["curl", "--version"], doctor.C_LOCALE_ENV)]
