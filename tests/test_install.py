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


def test_run_install_link_creates_launcher_symlink(monkeypatch, tmp_path):
    target_dir = tmp_path / "bin"
    monkeypatch.setenv("TOPO_LINK_DIR", str(target_dir))

    assert run_install_link(silent=True) is True

    target_link = target_dir / "topo"
    assert target_link.is_symlink()
    assert target_link.resolve().name == "topo"
