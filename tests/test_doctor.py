from pathlib import Path
from unittest.mock import patch

from src.core import doctor
from src.core.system import CommandResult


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
        patch("src.core.doctor.get_install_root", return_value=install_root),
        patch("src.core.doctor.get_install_source", return_value="script"),
        patch("src.core.doctor._get_core_binary", return_value=None),
        patch("src.core.doctor.shutil.which", return_value=None),
        patch("src.core.doctor.Path.home", return_value=home),
        patch(
            "src.core.doctor.run_command",
            return_value=_command_result(["sudo"], returncode=1),
        ),
    ):
        doctor.run_doctor()

    output = capsys.readouterr().out
    assert "Unavailable (VERSION missing or unreadable)" in output
    assert "Diagnostic complete." in output


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
        patch("src.core.doctor.get_install_root", return_value=install_root),
        patch("src.core.doctor.get_install_source", return_value="script"),
        patch("src.core.doctor._get_core_binary", return_value=engine),
        patch("src.core.doctor.shutil.which", return_value=None),
        patch("src.core.doctor.Path.home", return_value=home),
        patch("src.core.doctor.run_command", side_effect=fake_run_command),
    ):
        doctor.run_doctor()

    assert len(engine_calls) == 2
    assert all(
        timeout == doctor.DOCTOR_COMMAND_TIMEOUT for _args, _capture, timeout in engine_calls
    )
