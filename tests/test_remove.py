from unittest.mock import MagicMock, patch

from src.manage.remove import (
    _launcher_points_to_package,
    _launcher_points_to_topo,
    _strip_topo_path_lines,
    run_remove,
)


@patch("src.manage.remove.get_install_source", return_value="package")
@patch(
    "src.manage.remove.get_package_remove_argv",
    return_value=["sudo", "apt", "remove", "-y", "topo"],
)
@patch("src.manage.remove.subprocess.run")
def test_run_remove_executes_package_manager_removal(
    mock_run, _mock_command, _mock_install_source, monkeypatch, test_env, capsys
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
    mock_run.assert_called_once_with(["sudo", "apt", "remove", "-y", "topo"])


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
