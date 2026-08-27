import signal
import subprocess
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from src.core import system
from src.core.system import CommandResult, run_command, setup_passwordless_sudo


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
        env=None,
        stdin=None,
    )


@patch("subprocess.run")
def test_run_command_keeps_stdin_unless_detaching_is_asked_for(mock_run):
    """A child that inherits stdin can read the keystrokes a TUI is waiting for,
    but `sudo` asking for a password needs that stdin, so detaching is opt-in."""
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

    run_command(["-v"], use_sudo=True)
    assert mock_run.call_args.kwargs["stdin"] is None

    run_command(["xdg-open", "/tmp/x"], detach_stdin=True)
    assert mock_run.call_args.kwargs["stdin"] is subprocess.DEVNULL


@patch("subprocess.run")
def test_run_command_overlays_env_instead_of_replacing_it(mock_run, monkeypatch):
    """An env override is layered onto the inherited one.

    Replacing the environment outright would strip PATH, HOME and DISPLAY, which
    the tools being called need; the override only has to win where it overlaps.
    """
    mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LC_ALL", "zh_CN.UTF-8")

    run_command(["snap", "list"], env=system.C_LOCALE_ENV)

    child_env = mock_run.call_args.kwargs["env"]
    assert child_env["PATH"] == "/usr/bin"
    assert child_env["LC_ALL"] == "C"
    assert child_env["LANGUAGE"] == "C"
    assert child_env["LANG"] == "C"


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


@contextmanager
def _as_terminal():
    """Patch the state that makes the rewind sequence reachable at all.

    CLEAR_LINE and ERASE_BELOW are resolved when src.core.system is imported, and
    under pytest stdout is never a terminal, so both arrive empty. Restoring them
    here is what lets these tests assert on the sequence a real terminal gets.
    """
    with (
        patch("sys.stdout.isatty", return_value=True),
        patch("src.core.system.CLEAR_LINE", "\r\033[K"),
        patch("src.core.system.ERASE_BELOW", "\033[J"),
    ):
        yield


def test_ensure_sudo_session_clears_prompt_line_on_keyboard_interrupt():
    system.SUDO_CANCELLED = False
    with (
        patch(
            "src.core.system.run_command",
            side_effect=[
                CommandResult(["sudo", "-k"], 0),
                CommandResult(["sudo", "-n", "true"], 1),
                KeyboardInterrupt,
            ],
        ),
        _as_terminal(),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        assert system.ensure_sudo_session("Password: ") is False

    assert system.SUDO_CANCELLED is True
    clear_sequence = write.call_args.args[0]
    assert clear_sequence.startswith("\r\033[K")
    assert clear_sequence.endswith("\033[J")
    # A one-line prompt is cleared where the cursor already is: no rewind at all.
    # This used to rewind eight lines past it, guessing at the caller's frame.
    assert clear_sequence.count("\033[1A") == 0
    system.SUDO_CANCELLED = False


def test_ensure_sudo_session_rewinds_one_line_per_extra_prompt_line():
    """Only sudo's own prompt is erased -- one rewind per line above the cursor."""
    system.SUDO_CANCELLED = False
    with (
        patch(
            "src.core.system.run_command",
            side_effect=[
                CommandResult(["sudo", "-k"], 0),
                CommandResult(["sudo", "-n", "true"], 1),
                KeyboardInterrupt,
            ],
        ),
        _as_terminal(),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        assert system.ensure_sudo_session("Admin access needed\nPassword: ") is False

    assert write.call_args.args[0].count("\033[1A") == 1
    system.SUDO_CANCELLED = False


def test_ensure_sudo_session_rewinds_nothing_without_a_terminal():
    # Rewinding needs a cursor. CLEAR_LINE and ERASE_BELOW empty themselves off a
    # terminal, but the \033[1A between them is a literal, so this path needs its
    # own guard or a redirected run collects one cursor-up per rewound line.
    system.SUDO_CANCELLED = False
    with (
        patch(
            "src.core.system.run_command",
            side_effect=[
                CommandResult(["sudo", "-k"], 0),
                CommandResult(["sudo", "-n", "true"], 1),
                KeyboardInterrupt,
            ],
        ),
        patch("sys.stdout.isatty", return_value=False),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        assert system.ensure_sudo_session("Password: ") is False

    assert system.SUDO_CANCELLED is True
    assert write.call_args_list == []
    system.SUDO_CANCELLED = False


def test_ensure_sudo_session_treats_sigint_return_as_user_cancel():
    system.SUDO_CANCELLED = False
    with (
        patch(
            "src.core.system.run_command",
            side_effect=[
                CommandResult(["sudo", "-k"], 0),
                CommandResult(["sudo", "-n", "true"], 1),
                CommandResult(["sudo", "-v"], -signal.SIGINT),
            ],
        ),
        _as_terminal(),
        patch("sys.stdout.write") as write,
        patch("sys.stdout.flush"),
    ):
        assert system.ensure_sudo_session("Password: ") is False

    assert system.SUDO_CANCELLED is True
    clear_sequence = write.call_args.args[0]
    assert clear_sequence.startswith("\r\033[K")
    assert clear_sequence.endswith("\033[J")
    assert clear_sequence.count("\033[1A") == 0
    system.SUDO_CANCELLED = False


def test_setup_passwordless_sudo_uses_invoking_user(monkeypatch, capsys):
    monkeypatch.setenv("SUDO_USER", "realuser")
    monkeypatch.setenv("USER", "root")
    monkeypatch.setattr("sys.argv", ["/usr/bin/topo"])

    with patch("pathlib.Path.lstat") as mock_lstat:
        mock_lstat.return_value = MagicMock(st_uid=0, st_mode=0o755)
        assert setup_passwordless_sudo() is True

    out = capsys.readouterr().out
    assert "realuser ALL=(ALL) NOPASSWD: /usr/bin/topo" in out


def test_setup_passwordless_sudo_refuses_path_with_spaces(monkeypatch, capsys):
    monkeypatch.setenv("SUDO_USER", "realuser")
    monkeypatch.setattr("sys.argv", ["/home/real user/.topo/topo"])

    # No usable rule was produced, so `topo authorize` must not report success.
    assert setup_passwordless_sudo() is False

    out = capsys.readouterr().out
    assert "special characters or spaces" in out
    assert "NOPASSWD" not in out


def test_setup_passwordless_sudo_refuses_a_user_writable_script(monkeypatch, capsys):
    # NOPASSWD on a script the user can rewrite is a local root escalation, so
    # this path prints the safe per-binary alternative instead -- and still
    # reports failure, because the rule that was asked for was refused.
    monkeypatch.setenv("SUDO_USER", "realuser")
    monkeypatch.setattr("sys.argv", ["/home/realuser/.topo/topo"])

    with patch("pathlib.Path.lstat") as mock_lstat:
        mock_lstat.return_value = MagicMock(st_uid=1000, st_mode=0o755)
        assert setup_passwordless_sudo() is False

    out = capsys.readouterr().out
    assert "Refusing NOPASSWD rule" in out
    assert "ALL=(ALL) NOPASSWD" not in out
    assert "NOPASSWD: /usr/sbin/fstrim -a" in out
