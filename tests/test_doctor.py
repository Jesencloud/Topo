from pathlib import Path
from unittest.mock import patch

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
    assert "Diagnostic complete: 2 problem(s) found." in output
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
    assert "Diagnostic complete: 1 problem(s) found." in output


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
    assert "Diagnostic complete: 1 problem(s) found." in output
