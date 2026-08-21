import os
import subprocess
import tarfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = REPO_ROOT / "install.sh"
KEY_FINGERPRINT = "4B35C17CF8E663732726A99F50086DB998B4D883"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _make_release_fixture(tmp_path: Path) -> Path:
    fixture = tmp_path / "release"
    fixture.mkdir()
    source_root = tmp_path / "source"
    (source_root / "src/core/bin").mkdir(parents=True)
    (source_root / "src/main.py").write_text("# fixture\n")
    (source_root / "VERSION").write_text("1.1.0\n")
    (source_root / "topo").write_text("#!/bin/sh\nexit 0\n")
    source_archive = fixture / "topo-src.tar.gz"
    with tarfile.open(source_archive, "w:gz") as archive:
        archive.add(source_root, arcname="topo-src")
    (fixture / "topo-core-x86_64").write_bytes(b"fixture engine")
    (fixture / "topo-release-public.asc").write_text("fixture key\n")
    sums = []
    for name in ("topo-src.tar.gz", "topo-core-x86_64"):
        import hashlib

        sums.append(f"{hashlib.sha256((fixture / name).read_bytes()).hexdigest()}  {name}")
    (fixture / "SHA256SUMS").write_text("\n".join(sums) + "\n")
    (fixture / "SHA256SUMS.asc").write_text("fixture signature\n")
    return fixture


def _run_installer(
    tmp_path: Path, fixture: Path, curl_mode: str
) -> subprocess.CompletedProcess[str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    curl_script = f'''#!/bin/sh
set -eu
url="$1"
output=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then output="$2"; shift 2; continue; fi
  shift
done
name="$(basename "$url")"
if [ "{curl_mode}" = "engine-failure" ] && [ "$name" = "topo-core-x86_64" ]; then exit 22; fi
cp "{fixture}/$name" "$output"
'''
    _write_executable(fake_bin / "curl", curl_script)
    gpg_script = f"""#!/bin/sh
if [ "$1" = "--import" ]; then exit 0; fi
printf '[GNUPG:] VALIDSIG {KEY_FINGERPRINT} 20260101 0 4 0 1 10 00 {KEY_FINGERPRINT}\n'
exit 0
"""
    _write_executable(fake_bin / "gpg", gpg_script)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "NO_COLOR": "1",
            "TOPO_LINK_DIR": str(tmp_path / "home/.local/bin"),
        }
    )
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--version", "v1.1.0", "--minimal"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_missing_release_manifest_preserves_existing_install(tmp_path):
    fixture = _make_release_fixture(tmp_path)
    (fixture / "SHA256SUMS").unlink()
    home = tmp_path / "home"
    home.mkdir()
    existing = home / ".topo"
    existing.mkdir()
    (existing / "VERSION").write_text("1.0.9\n")

    result = _run_installer(tmp_path, fixture, "manifest-failure")

    assert result.returncode != 0
    assert (existing / "VERSION").read_text() == "1.0.9\n"
    assert not list(home.glob(".topo.install.*"))


def test_engine_download_failure_preserves_existing_install(tmp_path):
    fixture = _make_release_fixture(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    existing = home / ".topo"
    existing.mkdir()
    (existing / "VERSION").write_text("1.0.9\n")
    launcher_dir = home / ".local/bin"
    launcher_dir.mkdir(parents=True)
    (launcher_dir / "topo").symlink_to(existing / "topo")

    result = _run_installer(tmp_path, fixture, "engine-failure")

    assert result.returncode != 0
    assert (existing / "VERSION").read_text() == "1.0.9\n"
    assert (launcher_dir / "topo").resolve() == (existing / "topo").resolve()
    assert not list(home.glob(".topo.install.*"))
