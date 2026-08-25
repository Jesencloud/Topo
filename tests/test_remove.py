from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core.file_ops import validate_path_for_deletion
from src.manage.remove import (
    _launcher_points_to_package,
    _launcher_points_to_topo,
    _remove_package_user_residue,
    _strip_topo_path_lines,
    run_remove,
)


def _do_remove(path):
    import shutil

    p = Path(path)
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    elif p.exists() or p.is_symlink():
        p.unlink()
    return True


@patch("src.manage.remove.get_install_source", return_value="package")
@patch(
    "src.manage.remove.get_package_remove_argv",
    return_value=["sudo", "apt", "remove", "-y", "topo"],
)
@patch("src.manage.remove.subprocess.run")
@patch("src.manage.remove.safe_remove", side_effect=lambda p, **kw: (_do_remove(p), "ok"))
def test_run_remove_executes_package_manager_removal(
    _mock_safe, mock_run, _mock_command, _mock_install_source, monkeypatch, test_env, capsys
):
    mock_run.return_value = MagicMock(returncode=0)
    config_dir = test_env / ".config/topo"
    cache_dir = test_env / ".cache/topo"
    state_dir = test_env / ".local/state/topo"
    script_dir = test_env / ".topo"
    launcher_dir = test_env / ".local/bin"
    for path in (config_dir, cache_dir, state_dir, script_dir, launcher_dir):
        path.mkdir(parents=True)
    (config_dir / "config.json").write_text("{}")
    (cache_dir / "cache").write_text("")
    (state_dir / "history.json").write_text("[]")
    (script_dir / "topo").write_text("#!/bin/sh\n")
    launcher = launcher_dir / "topo"
    launcher.write_text("#!/bin/sh\n# Managed by topo package compatibility launcher.\n")

    monkeypatch.setenv("XDG_STATE_HOME", str(test_env / ".local/state"))
    monkeypatch.setattr("pathlib.Path.home", lambda: test_env)

    run_remove()

    output = capsys.readouterr().out
    assert "sudo apt remove -y topo" in output
    assert "Topo package removal completed" in output
    assert "Removed Configuration and whitelist" in output
    assert not config_dir.exists()
    assert not cache_dir.exists()
    assert not state_dir.exists()
    assert not script_dir.exists()
    assert not launcher.exists()
    mock_run.assert_called_once_with(["sudo", "apt", "remove", "-y", "topo"], timeout=300)


@patch("src.manage.remove.get_install_source", return_value="package")
@patch(
    "src.manage.remove.get_package_remove_argv",
    return_value=["sudo", "apt", "remove", "-y", "topo"],
)
@patch("src.manage.remove.subprocess.run")
@patch("src.manage.remove.safe_remove", side_effect=lambda p, **kw: (_do_remove(p), "ok"))
def test_package_removal_does_not_report_a_lock_only_config_dir(
    _mock_safe, mock_run, _mock_command, _mock_install_source, monkeypatch, test_env, capsys
):
    # The lock this run holds lives in ~/.config/topo, so the directory exists
    # even on a machine that never had user configuration. It still gets deleted
    # (it is residue this run created), but it must not be announced as removed
    # configuration.
    mock_run.return_value = MagicMock(returncode=0)
    config_dir = test_env / ".config/topo"
    config_dir.mkdir(parents=True)
    (config_dir / "topo.lock").write_text("4242\n")
    monkeypatch.setenv("XDG_STATE_HOME", str(test_env / ".local/state"))
    monkeypatch.setattr("pathlib.Path.home", lambda: test_env)

    run_remove()

    output = capsys.readouterr().out
    assert "Topo package removal completed" in output
    assert "Removed Configuration and whitelist" not in output
    assert not config_dir.exists()


@patch("src.manage.remove.get_install_source", return_value="package")
@patch(
    "src.manage.remove.get_package_remove_argv",
    return_value=["sudo", "dnf", "remove", "-y", "topo"],
)
@patch("src.manage.remove.subprocess.run")
def test_run_remove_dry_run_does_not_execute_package_manager(
    mock_run, _mock_command, _mock_install_source, capsys
):
    run_remove(dry_run=True)

    output = capsys.readouterr().out
    assert "sudo dnf remove -y topo" in output
    assert "Dry run complete" in output
    mock_run.assert_not_called()


def test_strip_topo_path_lines(test_env):
    bashrc = test_env / ".bashrc"
    bashrc.write_text(
        'export EDITOR=vim\n\n# Added by topo\nexport PATH="$HOME/.local/bin:$PATH"\n'
    )
    with patch("pathlib.Path.home", return_value=test_env):
        changed = _strip_topo_path_lines()

    assert changed is True
    content = bashrc.read_text()
    assert "# Added by topo" not in content
    assert "$HOME/.local/bin" not in content
    assert "export EDITOR=vim" in content  # unrelated lines preserved


def test_strip_topo_path_lines_noop_without_marker(test_env):
    bashrc = test_env / ".bashrc"
    bashrc.write_text("export EDITOR=vim\n")
    with patch("pathlib.Path.home", return_value=test_env):
        changed = _strip_topo_path_lines()
    assert changed is False
    assert bashrc.read_text() == "export EDITOR=vim\n"


def test_launcher_points_to_topo_handles_dangling_link(test_env):
    internal = test_env / ".topo"
    internal.mkdir()
    launcher_dir = test_env / ".local/bin"
    launcher_dir.mkdir(parents=True)

    launcher = launcher_dir / "topo"
    launcher.symlink_to(internal / "topo")  # dangling: target not created yet
    assert _launcher_points_to_topo(launcher, internal) is True

    other = launcher_dir / "other"
    other.symlink_to(test_env / "elsewhere")
    assert _launcher_points_to_topo(other, internal) is False


def test_launcher_points_to_package_ignores_binary_user_file(test_env):
    launcher = test_env / ".local/bin/topo"
    launcher.parent.mkdir(parents=True)
    launcher.write_bytes(b"\xff\xfe\x00custom")

    assert _launcher_points_to_package(launcher) is False


def test_validate_path_for_deletion_allows_self_removal(test_env):
    config_dir = test_env / ".config/topo"
    install_dir = test_env / ".topo"
    config_dir.mkdir(parents=True)
    install_dir.mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.core.whitelist.get_config_dir", return_value=config_dir),
        patch("src.core.whitelist.get_install_root", return_value=install_dir),
    ):
        from src.core.whitelist import get_hard_protection_reason_cached

        get_hard_protection_reason_cached.cache_clear()

        ok_config, reason_config = validate_path_for_deletion(
            config_dir, allow_app_data_removal=True
        )
        assert ok_config is False
        assert "Topo configuration" in reason_config
        ok_install, reason_install = validate_path_for_deletion(
            install_dir, allow_app_data_removal=True
        )
        assert ok_install is False
        assert "Topo installation" in reason_install
        ok_config_self, _ = validate_path_for_deletion(
            config_dir, allow_app_data_removal=True, allow_self_removal=True
        )
        assert ok_config_self is True
        ok_install_self, _ = validate_path_for_deletion(
            install_dir, allow_app_data_removal=True, allow_self_removal=True
        )
        assert ok_install_self is True
        get_hard_protection_reason_cached.cache_clear()


def test_launcher_helpers_handle_invalid_and_package_links(test_env):
    internal = test_env / ".topo"
    launcher = test_env / "launcher"
    with patch("src.manage.remove.os.readlink", side_effect=OSError):
        assert _launcher_points_to_topo(launcher, internal) is False
    launcher.symlink_to("/usr/bin/topo")
    assert _launcher_points_to_package(launcher) is True
    broken = test_env / "broken"
    broken.symlink_to(test_env / "missing")
    with (
        patch("src.manage.remove._resolve_launcher_symlink", return_value=None),
        patch("src.manage.remove.os.readlink", side_effect=OSError),
    ):
        assert _launcher_points_to_package(broken) is False


def test_strip_path_lines_handles_read_write_errors(test_env):
    bashrc = test_env / ".bashrc"
    bashrc.write_text("# Added by topo\nexport PATH=x\n")
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch.object(Path, "read_text", side_effect=OSError),
    ):
        assert _strip_topo_path_lines() is False
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch.object(Path, "write_text", side_effect=OSError),
    ):
        assert _strip_topo_path_lines() is False


def test_package_residue_removes_matching_entries_and_path(test_env, monkeypatch):
    launcher = test_env / ".local/bin/topo"
    launcher.parent.mkdir(parents=True)
    launcher.symlink_to(test_env / ".topo/topo")
    (test_env / ".topo").mkdir()
    (test_env / ".config/topo").mkdir(parents=True)
    (test_env / ".bashrc").write_text("# Added by topo\nexport PATH=x\n")
    monkeypatch.setattr("pathlib.Path.home", lambda: test_env)
    with patch("src.manage.remove._remove_path", return_value=True):
        removed = _remove_package_user_residue()
    assert "Launcher compatibility entry" in removed
    assert "Shell PATH entry" in removed


@patch("src.manage.remove.get_install_source", return_value="package")
@patch("src.manage.remove.get_package_remove_argv", return_value=None)
def test_package_remove_unsupported_distribution(_argv, _source, capsys):
    run_remove()
    assert "Unsupported Linux distribution" in capsys.readouterr().out


@patch("src.manage.remove.get_install_source", return_value="package")
@patch("src.manage.remove.get_package_remove_argv", return_value=["sudo", "dnf", "remove", "topo"])
@patch("src.manage.remove.subprocess.run", side_effect=OSError("denied"))
def test_package_remove_subprocess_error(_run, _argv, _source, capsys):
    run_remove()
    assert "Package removal failed" in capsys.readouterr().out


@patch("src.manage.remove.get_install_source", return_value="package")
@patch("src.manage.remove.get_package_remove_argv", return_value=["sudo", "dnf", "remove", "topo"])
@patch("src.manage.remove.subprocess.run", return_value=MagicMock(returncode=1))
def test_package_remove_nonzero_exit(_run, _argv, _source, capsys):
    run_remove()
    assert "exit code 1" in capsys.readouterr().out


def test_user_remove_no_integration_and_dry_run(test_env, monkeypatch, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: test_env)
    with patch("src.manage.remove.get_install_source", return_value="script"):
        run_remove()
    assert "No system integration" in capsys.readouterr().out

    (test_env / ".config/topo").mkdir(parents=True)
    with (
        patch("src.manage.remove.get_install_source", return_value="script"),
        patch("src.manage.remove.get_size_fast", return_value=10),
    ):
        run_remove(dry_run=True)
    assert "Dry run complete" in capsys.readouterr().out


def test_config_dir_holding_only_the_lock_file_is_not_leftover(test_env, monkeypatch, capsys):
    # run_remove now runs under SingleInstanceLock, which creates
    # ~/.config/topo/topo.lock before the scan. That file alone must not make an
    # otherwise clean system look like it still has configuration to remove.
    monkeypatch.setattr("pathlib.Path.home", lambda: test_env)
    config_dir = test_env / ".config/topo"
    config_dir.mkdir(parents=True)
    (config_dir / "topo.lock").write_text("4242\n")

    with patch("src.manage.remove.get_install_source", return_value="script"):
        run_remove()
    assert "No system integration" in capsys.readouterr().out

    (config_dir / "config.json").write_text("{}")
    with (
        patch("src.manage.remove.get_install_source", return_value="script"),
        patch("src.manage.remove.get_size_fast", return_value=10),
    ):
        run_remove(dry_run=True)
    assert "Configuration and whitelist" in capsys.readouterr().out


def test_user_remove_cancel_and_success_with_error(test_env, monkeypatch, capsys):
    monkeypatch.setattr("pathlib.Path.home", lambda: test_env)
    (test_env / ".topo").mkdir()
    (test_env / ".config/topo").mkdir(parents=True)
    with (
        patch("src.manage.remove.get_install_source", return_value="script"),
        patch("src.manage.remove.get_size_fast", return_value=1),
        patch("sys.stdin.fileno", return_value=0),
        patch("termios.tcgetattr", return_value=[]),
        patch("tty.setraw"),
        patch("sys.stdin.read", return_value="n"),
        patch("src.manage.remove.terminal_state.restore_raw_state"),
    ):
        run_remove()
    assert "cancelled" in capsys.readouterr().out

    with (
        patch("src.manage.remove.get_install_source", return_value="script"),
        patch("src.manage.remove.get_size_fast", return_value=1),
        patch("sys.stdin.fileno", return_value=0),
        patch("termios.tcgetattr", return_value=[]),
        patch("tty.setraw"),
        patch("sys.stdin.read", return_value="\n"),
        patch("src.manage.remove.terminal_state.restore_raw_state"),
        patch("src.manage.remove.safe_remove", return_value=(False, "denied")),
    ):
        run_remove()
    assert "completed with errors" in capsys.readouterr().out
