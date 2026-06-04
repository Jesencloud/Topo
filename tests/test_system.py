import subprocess
from unittest.mock import MagicMock, patch

from src.core.system import run_command, setup_passwordless_sudo


@patch("subprocess.run")
def test_run_command_success_result(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

    result = run_command(["echo", "ok"], timeout=5)

    assert result.ok is True
    assert result.returncode == 0
    assert result.stdout == "ok"
    mock_run.assert_called_with(
        ["echo", "ok"],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


@patch("subprocess.run")
def test_run_command_failure_result(mock_run):
    mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="bad")

    result = run_command(["false"], timeout=5)

    assert result.ok is False
    assert result.returncode == 2
    assert result.stderr == "bad"


@patch("subprocess.run")
def test_run_command_timeout_result(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(["slow"], timeout=1, output=b"partial")

    result = run_command(["slow"], timeout=1)

    assert result.ok is False
    assert result.returncode == 124
    assert result.timed_out is True
    assert result.stdout == "partial"


def test_setup_passwordless_sudo_uses_invoking_user(monkeypatch, capsys):
    monkeypatch.setenv("SUDO_USER", "realuser")
    monkeypatch.setenv("USER", "root")
    monkeypatch.setattr("sys.argv", ["/home/realuser/.topo/topo"])

    setup_passwordless_sudo()

    out = capsys.readouterr().out
    assert "realuser ALL=(ALL) NOPASSWD: /home/realuser/.topo/topo" in out


def test_setup_passwordless_sudo_refuses_path_with_spaces(monkeypatch, capsys):
    monkeypatch.setenv("SUDO_USER", "realuser")
    monkeypatch.setattr("sys.argv", ["/home/real user/.topo/topo"])

    setup_passwordless_sudo()

    out = capsys.readouterr().out
    assert "NOPASSWD" not in out
    assert "Could not generate a safe sudoers rule" in out
