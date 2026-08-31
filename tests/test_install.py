import os
import re
import subprocess
import sys
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


def _fake_python3(tmp_path: Path, version: tuple[int, int, int]) -> Path:
    """A python3 on PATH that reports `version` and is otherwise the real one.

    install.sh asks two questions of the interpreter -- print the version, and
    exit non-zero if it is too old -- and a stub that pattern-matched those two
    command lines would pass even if the script stopped asking. This answers them
    by actually running the code install.sh passes, against a patched
    sys.version_info.
    """
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "python3"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        f"sys.version_info = {version!r}\n"
        'code = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "-c" else ""\n'
        "del sys.argv[1:]\n"
        "exec(code)\n"
    )
    stub.chmod(0o755)
    return bin_dir


def test_install_script_refuses_an_interpreter_older_than_the_code_requires(tmp_path):
    # The failure this closes: on Debian 11 (3.9) or RHEL 8 (3.6) the script
    # checked only that `python3` existed, ticked every box, printed its success
    # banner, and left behind a Topo that died on first run.
    bin_dir = _fake_python3(tmp_path, (3, 9, 2))

    result = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "install.sh"), "--minimal"],
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1
    assert "requires Python 3.10 or newer (found 3.9.2)" in result.stdout
    # And it stopped there: nothing was downloaded, nothing was installed.
    assert not (tmp_path / ".topo").exists()


def _run_installer_block(pattern: str, prelude: str, expr: str, env: dict, count: int = 1) -> str:
    """Run top-level blocks of install.sh in isolation.

    Same reason as _run_installer_link_helpers: none of this is visible to ruff,
    mypy, vulture or tach, and the alternative to extracting it is a text grep
    that passes whatever the block happens to say.
    """
    script = (REPO_ROOT / "install.sh").read_text()
    blocks = re.findall(pattern, script, re.M | re.S)
    assert len(blocks) == count, (
        f"install.sh no longer contains {count} block(s) matching {pattern!r}"
    )

    result = subprocess.run(
        ["/bin/bash", "-c", f"{prelude}\n" + "\n".join(blocks) + f"\n{expr}"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


CURL_RETRY_BLOCK = r"^CURL_RETRY_OPTS=\(.*?^fi$"


def _curl_retry_opts(tmp_path: Path, *, knows_retry_all_errors: bool) -> list[str]:
    """The flags install.sh settles on, against a curl that does or does not
    accept --retry-all-errors."""
    bin_dir = tmp_path / "curlbin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "curl"
    # Exit 2 with no output is what real curl does for an option it does not
    # know: `curl: option --retry-all-errors: is unknown`.
    stub.write_text(
        "#!/bin/sh\n"
        + (
            ""
            if knows_retry_all_errors
            else 'case " $* " in *" --retry-all-errors "*) exit 2;; esac\n'
        )
        + "exit 0\n"
    )
    stub.chmod(0o755)

    return _run_installer_block(
        CURL_RETRY_BLOCK,
        prelude="",
        expr='printf "%s\\n" "${CURL_RETRY_OPTS[@]}"',
        env={"HOME": str(tmp_path), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    ).split()


def test_installer_adds_retry_all_errors_when_curl_supports_it(tmp_path):
    """A TLS handshake the peer cuts off must be retried, not reported as fatal.

    curl's own --retry covers only what it calls transient -- timeouts, 408,
    429, 5xx -- plus ECONNREFUSED from --retry-connrefused. `curl: (35) TLS
    connect error: ... unexpected eof while reading` is in none of those sets,
    so an install behind a TLS-inspecting middlebox aborted on the first
    attempt while asking for three retries.
    """
    assert _curl_retry_opts(tmp_path, knows_retry_all_errors=True) == [
        "--connect-timeout",
        "10",
        "--retry",
        "3",
        "--retry-delay",
        "2",
        "--retry-connrefused",
        "--retry-all-errors",
    ]


def test_installer_omits_retry_all_errors_on_a_curl_too_old_for_it(tmp_path):
    # --retry-all-errors is curl 7.71+; RHEL 8 ships 7.61, where passing it
    # exits 2 before a single byte is fetched. Losing the extra retries there is
    # the point of probing -- turning a working install into an unknown-option
    # error is not.
    flags = _curl_retry_opts(tmp_path, knows_retry_all_errors=False)

    assert "--retry-all-errors" not in flags
    assert flags == [
        "--connect-timeout",
        "10",
        "--retry",
        "3",
        "--retry-delay",
        "2",
        "--retry-connrefused",
    ]


def test_every_release_download_carries_the_shared_retry_flags():
    """One retry policy for all five signed-release downloads.

    The flags used to be spelled out at each call site, so a sixth download --
    or a fix applied to four of the five -- would silently differ from the rest.
    Scoped to lines naming $RELEASE_URL rather than every curl in the file, so
    there is no exemption list to keep honest: the one other curl resolves the
    latest tag, has a python3 fallback when it fails, and downloads nothing.
    """
    downloads = [
        line.strip()
        for line in (REPO_ROOT / "install.sh").read_text().splitlines()
        if line.lstrip().startswith("curl ") and "$RELEASE_URL" in line
    ]

    assert len(downloads) == 5, downloads
    assert [line for line in downloads if '"${CURL_RETRY_OPTS[@]}"' not in line] == []


def _installer_function(name: str) -> str:
    """One named shell function of install.sh, verbatim."""
    script = (REPO_ROOT / "install.sh").read_text()
    block = re.search(rf"^{name}\(\) \{{\n.*?^\}}$", script, re.M | re.S)
    assert block is not None, f"install.sh no longer defines {name}()"
    return block.group(0)


ENGINE_SELECTION = r"^ENGINE_NAME=\$\(engine_for_arch \"\$ARCH\"\)\n.*?^fi$"


@pytest.mark.parametrize(
    ("arch", "kept"),
    [
        ("x86_64", "topo-core-x86_64"),
        ("aarch64", "topo-core-aarch64"),
        ("arm64", "topo-core-aarch64"),
    ],
)
def test_install_script_installs_only_the_engine_for_this_architecture(tmp_path, arch, kept):
    bin_dir = tmp_path / "src/core/bin"
    bin_dir.mkdir(parents=True)
    for name in ("topo-core-x86_64", "topo-core-aarch64"):
        (bin_dir / name).write_text("shipped in the source archive\n")

    output = _run_installer_block(
        ENGINE_SELECTION,
        prelude=(
            f'ARCH={arch}\nBIN_DIR="{bin_dir}"\nMINIMAL=false\n'
            + _installer_function("engine_for_arch")
            + '\nfetch_engine_binary() { printf downloaded > "$BIN_DIR/$1"; }\n'
        ),
        expr=":",
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
    )

    assert (bin_dir / kept).read_text() == "downloaded"
    assert sorted(p.name for p in bin_dir.iterdir()) == [kept]
    assert "pure-Python" not in output


def test_install_script_removes_both_engines_on_an_architecture_without_one(tmp_path):
    # The source archive carries both engines; leaving either one behind hands
    # Topo a binary the kernel refuses to exec (src/core/engine.py agrees this
    # architecture has no engine, and falls back for the same reason).
    bin_dir = tmp_path / "src/core/bin"
    bin_dir.mkdir(parents=True)
    for name in ("topo-core-x86_64", "topo-core-aarch64"):
        (bin_dir / name).write_text("shipped in the source archive\n")

    output = _run_installer_block(
        ENGINE_SELECTION,
        prelude=(
            f'ARCH=riscv64\nBIN_DIR="{bin_dir}"\nMINIMAL=false\n'
            + _installer_function("engine_for_arch")
            + '\nfetch_engine_binary() { printf downloaded > "$BIN_DIR/$1"; }\n'
        ),
        expr=":",
        env={"HOME": str(tmp_path), "PATH": os.environ["PATH"]},
    )

    assert list(bin_dir.iterdir()) == []
    assert "No prebuilt engine for riscv64" in output
    assert "pure-Python path" in output


VERSION_MATCH_HELPERS = (
    r"^(?:resolve_link_target_dir|absolute_link_dir|engine_for_arch"
    r"|installed_version_matches)\(\) \{\n.*?^\}$"
)


def _complete_install(tmp_path: Path, version: str, engines: tuple[str, ...]) -> dict:
    """A ~/.topo that installed_version_matches() should be happy with."""
    tree = tmp_path / ".topo"
    (tree / "src/core/bin").mkdir(parents=True)
    (tree / "VERSION").write_text(f"{version}\n")
    launcher = tree / "topo"
    launcher.write_text("#!/usr/bin/env python3\n")
    launcher.chmod(0o755)
    (tree / "src/main.py").write_text("")
    for name in engines:
        binary = tree / "src/core/bin" / name
        binary.write_text("engine")
        binary.chmod(0o755)
    link_dir = tmp_path / "bin"
    link_dir.mkdir()
    (link_dir / "topo").symlink_to(launcher)
    return {
        "HOME": str(tmp_path),
        "TARGET_REF": f"v{version}",
        "TOPO_LINK_DIR": str(link_dir),
        "PATH": f"{link_dir}{os.pathsep}{os.environ['PATH']}",
    }


def _installed_version_matches(env: dict, arch: str, tmp_path: Path) -> str:
    fake_bin = tmp_path / "unamebin"
    fake_bin.mkdir(exist_ok=True)
    uname = fake_bin / "uname"
    uname.write_text(f"#!/bin/sh\necho {arch}\n")
    uname.chmod(0o755)
    env = {**env, "PATH": f"{fake_bin}{os.pathsep}{env['PATH']}"}

    return _run_installer_block(
        VERSION_MATCH_HELPERS,
        prelude="",
        expr="if installed_version_matches; then echo MATCH; else echo MISS; fi",
        env=env,
        count=4,
    ).strip()


@pytest.mark.parametrize(
    ("arch", "engine_name"),
    [("x86_64", "topo-core-x86_64"), ("aarch64", "topo-core-aarch64")],
)
def test_a_repeat_install_is_skipped_only_when_this_arch_engine_is_there(
    tmp_path, arch, engine_name
):
    env = _complete_install(tmp_path, "1.1.2", engines=(engine_name,))
    assert _installed_version_matches(env, arch, tmp_path) == "MATCH"

    (tmp_path / ".topo/src/core/bin" / engine_name).unlink()
    assert _installed_version_matches(env, arch, tmp_path) == "MISS"


def test_a_repeat_install_is_skipped_on_an_architecture_that_has_no_engine(tmp_path):
    # This used to return 1 unconditionally for an unsupported architecture, so
    # every single run re-downloaded and re-installed the whole release -- for a
    # file step 4 had just deliberately deleted.
    env = _complete_install(tmp_path, "1.1.2", engines=())

    assert _installed_version_matches(env, "riscv64", tmp_path) == "MATCH"


def _run_installer_link_helpers(env, expr="resolve_link_target_dir"):
    """Evaluate install.sh's launcher-path helpers without running the installer.

    The script must not import get_link_target_dir() from the tree it installs
    (that tree is an arbitrary older release -- doing so raised ImportError and
    aborted the install), so it reimplements the rule in shell. Nothing else
    would notice the two drifting apart: ruff, mypy, vulture and tach never read
    shell. This extracts the two functions and runs them.
    """
    return _run_installer_block(
        r"^(?:resolve_link_target_dir|absolute_link_dir)\(\) \{\n.*?^\}$",
        prelude="",
        expr=expr,
        env=env,
        count=2,
    ).strip()


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


def test_run_install_link_appends_to_a_non_utf8_rc_file(monkeypatch, tmp_path, test_env):
    """A GBK .bashrc gets the PATH block appended and keeps its own bytes.

    `topo link` used to read the file with a strict decode, so one non-UTF-8
    comment raised UnicodeDecodeError -- a ValueError, past the `except OSError`
    -- and `topo link` died without configuring anything. The read is only
    looking for an ASCII export line, so errors="replace" is enough here; the
    bytes already in the file are never rewritten, because the block is appended
    rather than the whole file replaced.
    """
    target_dir = tmp_path / "bin"
    monkeypatch.setenv("TOPO_LINK_DIR", str(target_dir))
    monkeypatch.setenv("PATH", "/usr/bin")
    bashrc = test_env / ".bashrc"
    original = "# 中文注释\n".encode("gbk")
    bashrc.write_bytes(original)

    assert run_install_link(silent=True) is True

    after = bashrc.read_bytes()
    assert after.startswith(original)
    assert b"# Added by topo" in after
    assert f'export PATH="{target_dir}:$PATH"'.encode() in after


def test_run_install_link_writes_a_non_utf8_link_dir_verbatim(monkeypatch, tmp_path, test_env):
    """A TOPO_LINK_DIR whose name is not UTF-8 lands in .bashrc as its own bytes.

    The export line embeds an installation path, and TOPO_LINK_DIR reaches Python
    already decoded with surrogateescape, so a directory named with a stray 0xff
    arrives as a lone surrogate. Encoding that strictly raises UnicodeEncodeError
    -- a ValueError, missed by `except OSError` just as the decode was -- so the
    append writes with surrogateescape and bash gets the bytes back unchanged.
    """
    target_dir = tmp_path / os.fsdecode(b"b\xffn")
    monkeypatch.setenv("TOPO_LINK_DIR", str(target_dir))
    monkeypatch.setenv("PATH", "/usr/bin")
    bashrc = test_env / ".bashrc"
    bashrc.write_text("# existing config\n")

    assert run_install_link(silent=True) is True

    after = bashrc.read_bytes()
    assert b"# Added by topo" in after
    assert os.fsencode(f'export PATH="{target_dir}:$PATH"') in after


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
