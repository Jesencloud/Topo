import subprocess
from hashlib import sha256
from unittest.mock import MagicMock, patch

from src.manage.update import (
    _download_file,
    _fetch_latest_release_tag,
    _should_update,
    _verify_release_checksum,
    run_update,
)


def test_should_update_uses_semantic_version_ordering():
    assert _should_update("1.9.0", "1.10.0") is True
    assert _should_update("1.9.0", "v1.10.0") is True
    assert _should_update("1.10.0", "1.9.0") is False
    assert _should_update("1.10.0", "1.10.0") is False
    assert _should_update("1.10.0", "not-a-version") is False


@patch("src.manage.update.subprocess.check_output")
def test_fetch_latest_release_tag_uses_api_headers(mock_check_output):
    mock_check_output.return_value = '{"tag_name": "v1.2.3"}'

    assert _fetch_latest_release_tag() == "v1.2.3"

    argv = mock_check_output.call_args.args[0]
    assert "Accept: application/vnd.github+json" in argv
    assert "User-Agent: topo-updater" in argv
    assert mock_check_output.call_args.kwargs["stderr"] is subprocess.DEVNULL


@patch("src.manage.update.subprocess.check_output")
def test_fetch_latest_release_tag_falls_back_to_release_redirect(mock_check_output):
    mock_check_output.side_effect = [
        subprocess.CalledProcessError(22, ["curl"]),
        "https://github.com/Jesencloud/Topo/releases/tag/v1.2.3",
    ]

    assert _fetch_latest_release_tag() == "v1.2.3"

    fallback_argv = mock_check_output.call_args_list[1].args[0]
    assert "https://github.com/Jesencloud/Topo/releases/latest" in fallback_argv
    assert "topo-updater" in fallback_argv
    assert mock_check_output.call_args_list[0].kwargs["stderr"] is subprocess.DEVNULL
    assert mock_check_output.call_args_list[1].kwargs["stderr"] is subprocess.DEVNULL


@patch("src.manage.update.subprocess.check_call")
def test_download_file_uses_user_agent(mock_check_call, tmp_path):
    destination = tmp_path / "asset"

    _download_file("https://example.test/asset", destination)

    argv = mock_check_call.call_args.args[0]
    assert "-A" in argv
    assert "topo-updater" in argv


def test_verify_release_checksum_accepts_matching_asset(tmp_path):
    package = tmp_path / "topo-1.2.3-1.x86_64.rpm"
    package.write_bytes(b"package")
    checksum = sha256(b"package").hexdigest()
    sha256sums = tmp_path / "SHA256SUMS"
    sha256sums.write_text(f"{checksum}  {package.name}\n")

    assert _verify_release_checksum(package, sha256sums) is True


def test_verify_release_checksum_rejects_mismatch(tmp_path):
    package = tmp_path / "topo-1.2.3-1.x86_64.rpm"
    package.write_bytes(b"package")
    sha256sums = tmp_path / "SHA256SUMS"
    sha256sums.write_text(f"{'0' * 64}  {package.name}\n")

    assert _verify_release_checksum(package, sha256sums) is False


@patch("src.manage.update.get_install_source", return_value="package")
@patch("src.manage.update._read_local_version", return_value="0.9.1")
@patch("src.manage.update._fetch_latest_release_tag", return_value="v0.9.3")
@patch("src.manage.update.get_package_asset_name", return_value="topo-0.9.3-1.x86_64.rpm")
@patch("src.manage.update.get_package_upgrade_argv")
@patch("src.manage.update.subprocess.run")
def test_run_update_downloads_and_installs_package_update(
    mock_run,
    mock_upgrade_argv,
    _mock_asset_name,
    _mock_remote_tag,
    _mock_local_version,
    _mock_install_source,
    monkeypatch,
    capsys,
):
    package_bytes = b"rpm package"

    def fake_download(_url, destination, timeout=60):
        if destination.name == "SHA256SUMS":
            checksum = sha256(package_bytes).hexdigest()
            destination.write_text(f"{checksum}  topo-0.9.3-1.x86_64.rpm\n")
        else:
            destination.write_bytes(package_bytes)

    monkeypatch.setattr("src.manage.update._download_file", fake_download)
    mock_upgrade_argv.side_effect = lambda package_path: [
        "sudo",
        "dnf",
        "upgrade",
        "-y",
        str(package_path),
    ]
    mock_run.return_value = MagicMock(returncode=0)

    run_update()

    output = capsys.readouterr().out
    assert "New package available: v0.9.3" in output
    assert "Verified SHA256 for topo-0.9.3-1.x86_64.rpm" in output
    mock_run.assert_called_once()
    argv = mock_run.call_args.args[0]
    assert argv[:4] == ["sudo", "dnf", "upgrade", "-y"]
    assert argv[4].endswith("topo-0.9.3-1.x86_64.rpm")


@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_does_not_install_when_remote_is_older(mock_check_output, mock_run):
    mock_check_output.return_value = '{"tag_name": "v0.0.1"}'

    run_update()

    mock_run.assert_not_called()


@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_does_not_install_when_remote_version_is_invalid(mock_check_output, mock_run):
    mock_check_output.return_value = '{"tag_name": "latest"}'

    run_update()

    mock_run.assert_not_called()


@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_installs_only_when_remote_is_newer(mock_check_output, mock_run):
    # 1st check_output fetches the release tag; 2nd downloads install.sh.
    mock_check_output.side_effect = ['{"tag_name": "v999.0.0"}', "#!/usr/bin/env bash\n"]
    mock_run.return_value = MagicMock(returncode=0)

    run_update()

    # Installer downloaded for the resolved tag (curl invoked as an argv list).
    download_argv = mock_check_output.call_args_list[1].args[0]
    assert any("Topo/v999.0.0/install.sh" in part for part in download_argv)

    # Executed without a shell, with the tag as a separate argv element.
    mock_run.assert_called_once()
    argv = mock_run.call_args.args[0]
    assert argv == ["bash", "-s", "--", "--minimal", "--version", "v999.0.0"]
    assert mock_run.call_args.kwargs.get("shell", False) is False


@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_rejects_unsafe_tag(mock_check_output, mock_run):
    # An epoch tag like "1!2.3" parses as a version but contains '!'; it must be
    # refused before being used in a URL or handed to the installer.
    mock_check_output.return_value = '{"tag_name": "1!2.3"}'

    run_update()

    mock_run.assert_not_called()


@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_rejects_non_script_payload(mock_check_output, mock_run):
    # L2: a downloaded "installer" that isn't a script (e.g. a CDN/error page or
    # a truncated body) must never be piped into bash.
    mock_check_output.side_effect = [
        '{"tag_name": "v999.0.0"}',
        "<html><body>503 Service Unavailable</body></html>",
    ]

    run_update()

    mock_run.assert_not_called()
