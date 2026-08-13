import os
from pathlib import Path

from src.manage.install import _get_link_target_dir, run_install_link


def test_get_link_target_dir_uses_override(monkeypatch, tmp_path):
    target = tmp_path / "bin"
    monkeypatch.setenv("TOPO_LINK_DIR", str(target))

    assert _get_link_target_dir() == target


def test_get_link_target_dir_uses_usr_local_bin_for_root(monkeypatch):
    monkeypatch.delenv("TOPO_LINK_DIR", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    assert _get_link_target_dir() == Path("/usr/local/bin")


def test_run_install_link_creates_launcher_symlink(monkeypatch, tmp_path, test_env):
    # test_env isolates HOME: silent mode now also appends the PATH export to the
    # user's shell configs, so without it this test writes into the real ~/.bashrc.
    target_dir = tmp_path / "bin"
    monkeypatch.setenv("TOPO_LINK_DIR", str(target_dir))

    assert run_install_link(silent=True) is True

    target_link = target_dir / "topo"
    assert target_link.is_symlink()
    assert target_link.resolve().name == "topo"


def test_run_install_link_fixes_path_even_when_silent(monkeypatch, tmp_path, test_env, capsys):
    """Silent mode must still repair PATH, otherwise a first-time install leaves
    the shell unable to find `topo`. It must stay quiet while doing so, and must
    not create shell configs the user does not have."""
    target_dir = tmp_path / "bin"
    monkeypatch.setenv("TOPO_LINK_DIR", str(target_dir))
    monkeypatch.setenv("PATH", "/usr/bin")
    bashrc = test_env / ".bashrc"
    bashrc.write_text("# existing config\n")

    assert run_install_link(silent=True) is True

    content = bashrc.read_text()
    assert "# Added by topo" in content
    assert f'export PATH="{target_dir}:$PATH"' in content
    # Only pre-existing configs are touched, and silent stays silent.
    assert not (test_env / ".zshrc").exists()
    assert capsys.readouterr().out == ""


def test_run_install_link_does_not_duplicate_path_entry(monkeypatch, tmp_path, test_env):
    target_dir = tmp_path / "bin"
    monkeypatch.setenv("TOPO_LINK_DIR", str(target_dir))
    monkeypatch.setenv("PATH", "/usr/bin")
    bashrc = test_env / ".bashrc"
    bashrc.write_text("# existing config\n")

    assert run_install_link(silent=True) is True
    assert run_install_link(silent=True) is True

    assert bashrc.read_text().count("# Added by topo") == 1


def test_run_install_link_already_configured_but_not_in_path(
    monkeypatch, tmp_path, test_env, capsys
):
    """When the export_line is already present in .bashrc, but the current process PATH
    is not yet updated (in_path == False), run_install_link must recognize configured=True,
    added=False: it should report that configuration already exists (not 'Manual action required')
    and announce System setup complete."""
    target_dir = tmp_path / "bin"
    monkeypatch.setenv("TOPO_LINK_DIR", str(target_dir))
    monkeypatch.setenv("PATH", "/usr/bin")  # target_dir is NOT in current process PATH
    bashrc = test_env / ".bashrc"
    export_line = f'export PATH="{target_dir}:$PATH"'
    bashrc.write_text(f"# pre-existing config\n{export_line}\n")

    assert run_install_link(silent=False) is True

    out = capsys.readouterr().out
    assert "PATH configuration already exists in your shell config" in out
    assert "System setup complete" in out
    assert "Manual action required" not in out
    assert bashrc.read_text().count(export_line) == 1
