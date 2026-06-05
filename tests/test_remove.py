from unittest.mock import patch

from src.manage.remove import _launcher_points_to_topo, _strip_topo_path_lines, run_remove


@patch("src.manage.remove.get_install_source", return_value="package")
def test_run_remove_delegates_package_installs_to_package_manager(_mock_install_source, capsys):
    run_remove()

    output = capsys.readouterr().out
    assert "sudo apt remove topo" in output
    assert "sudo dnf remove topo" in output


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
