import os
import re
import subprocess
from pathlib import Path

import pytest

from src.core.paths import get_link_target_dir
from src.manage.install import run_install_link

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_install_script_fails_early_when_curl_is_missing(tmp_path):
    result = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "install.sh"), "--minimal"],
        env={"HOME": str(tmp_path), "PATH": str(tmp_path)},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "curl is required but not installed" in result.stdout
    assert "python3 is required" not in result.stdout


def _run_installer_link_helpers(env, expr="resolve_link_target_dir"):
    """Evaluate install.sh's launcher-path helpers without running the installer.

    The script must not import get_link_target_dir() from the tree it installs
    (that tree is an arbitrary older release -- doing so raised ImportError and
    aborted the install), so it reimplements the rule in shell. Nothing else
    would notice the two drifting apart: ruff, mypy, vulture and tach never read
    shell. This extracts the two functions and runs them.
    """
    script = (REPO_ROOT / "install.sh").read_text()
    blocks = re.findall(
        r"^(?:resolve_link_target_dir|absolute_link_dir)\(\) \{\n.*?^\}$", script, re.M | re.S
    )
    assert len(blocks) == 2, "install.sh no longer defines the launcher-path helpers by name"

    result = subprocess.run(
        ["/bin/bash", "-c", "\n".join(blocks) + "\n" + expr],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.mark.parametrize("override", [None, "/opt/bin", "~/xbin", "relbin"])
def test_install_script_resolves_the_same_launcher_dir_as_python(tmp_path, monkeypatch, override):
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    monkeypatch.setenv("HOME", str(tmp_path))
    if override is None:
        monkeypatch.delenv("TOPO_LINK_DIR", raising=False)
    else:
        env["TOPO_LINK_DIR"] = override
        monkeypatch.setenv("TOPO_LINK_DIR", override)

    assert _run_installer_link_helpers(env) == str(get_link_target_dir())


def test_install_script_matches_python_for_a_root_install(tmp_path, monkeypatch):
    # The one branch the test runner cannot be in: fake `id` for the shell side
    # and geteuid() for the Python side, and require both to say the same thing.
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_id = fake_bin / "id"
    fake_id.write_text("#!/bin/sh\necho 0\n")
    fake_id.chmod(0o755)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("TOPO_LINK_DIR", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    env = {"HOME": str(tmp_path), "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}

    assert _run_installer_link_helpers(env) == str(get_link_target_dir()) == "/usr/local/bin"


def test_install_script_puts_a_relative_override_under_the_install_tree(tmp_path):
    # get_link_target_dir() leaves a relative override relative because `topo
    # link` runs from ~/.topo; install.sh has to reach the same absolute path for
    # its own symlink verification and failure cleanup.
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "TOPO_LINK_DIR": "relbin"}

    resolved = _run_installer_link_helpers(env, 'absolute_link_dir "$(resolve_link_target_dir)"')

    assert resolved == str(tmp_path / ".topo/relbin")


def test_get_link_target_dir_uses_override(monkeypatch, tmp_path):
    target = tmp_path / "bin"
    monkeypatch.setenv("TOPO_LINK_DIR", str(target))

    assert get_link_target_dir() == target


def test_get_link_target_dir_uses_usr_local_bin_for_root(monkeypatch):
    monkeypatch.delenv("TOPO_LINK_DIR", raising=False)
    monkeypatch.setattr(os, "geteuid", lambda: 0)

    assert get_link_target_dir() == Path("/usr/local/bin")


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
