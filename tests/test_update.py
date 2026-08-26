import subprocess
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from packaging.version import Version

from src.manage.update import (
    TOPO_RELEASE_KEY_FINGERPRINT,
    _download_file,
    _expected_sha256,
    _fetch_latest_release_tag,
    _normalized_gpg_fingerprint,
    _parse_version,
    _release_signature_status_matches,
    _run_package_update,
    _subprocess_stderr_tail,
    _verify_release_checksum,
    _verify_release_signature,
    run_update,
)


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


@patch("src.manage.update.subprocess.run")
def test_download_file_uses_user_agent(mock_run, tmp_path):
    destination = tmp_path / "asset"

    def fake_run(argv, **_kwargs):
        partial = argv[-1]
        assert partial.endswith(".part")
        destination.with_name("asset.part").write_bytes(b"asset")
        return MagicMock(returncode=0, stderr="")

    mock_run.side_effect = fake_run

    _download_file("https://example.test/asset", destination)

    argv = mock_run.call_args.args[0]
    assert "-A" in argv
    assert "topo-updater" in argv
    assert destination.read_bytes() == b"asset"
    assert not destination.with_name("asset.part").exists()


@patch("src.manage.update.subprocess.run")
def test_download_file_retries_partial_transfer(mock_run, tmp_path, capsys):
    destination = tmp_path / "asset"

    def fake_run(argv, **_kwargs):
        partial = destination.with_name("asset.part")
        if mock_run.call_count == 1:
            partial.write_bytes(b"partial")
            return MagicMock(returncode=18, stderr="curl: (18) Transferred a partial file")
        partial.write_bytes(b"complete")
        return MagicMock(returncode=0, stderr="")

    mock_run.side_effect = fake_run

    _download_file("https://example.test/asset", destination)

    assert mock_run.call_count == 2
    assert destination.read_bytes() == b"complete"
    assert not destination.with_name("asset.part").exists()
    assert "Download interrupted, retrying (2/4)" in capsys.readouterr().out


@patch("src.manage.update.subprocess.run")
def test_download_file_removes_partial_after_failed_attempts(mock_run, tmp_path):
    destination = tmp_path / "asset"

    def fake_run(_argv, **_kwargs):
        destination.with_name("asset.part").write_bytes(b"partial")
        return MagicMock(returncode=18, stderr="curl: (18) Transferred a partial file")

    mock_run.side_effect = fake_run

    try:
        _download_file("https://example.test/asset", destination, attempts=2)
    except subprocess.CalledProcessError as e:
        assert e.returncode == 18
    else:
        raise AssertionError("Expected download failure")

    assert not destination.exists()
    assert not destination.with_name("asset.part").exists()


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


def test_release_signature_status_matches_pinned_primary_fingerprint():
    signing_subkey_fingerprint = "A" * 40
    output = (
        "[GNUPG:] NEWSIG\n"
        f"[GNUPG:] VALIDSIG {signing_subkey_fingerprint} "
        f"2026-06-09 1781000000 0 4 0 1 10 00 {TOPO_RELEASE_KEY_FINGERPRINT}\n"
    )

    assert _release_signature_status_matches(output) is True


def test_release_signature_status_rejects_foreign_primary_fingerprint():
    attacker_fingerprint = "A" * 40
    output = (
        "[GNUPG:] NEWSIG\n"
        f"[GNUPG:] VALIDSIG {attacker_fingerprint} "
        f"2026-06-09 1781000000 0 4 0 1 10 00 {attacker_fingerprint}\n"
    )

    assert _release_signature_status_matches(output) is False


@patch("src.manage.update.shutil.which", return_value=None)
def test_verify_release_signature_falls_back_without_gpg(_mock_which, tmp_path, capsys):
    assert (
        _verify_release_signature(
            tmp_path / "SHA256SUMS",
            tmp_path / "SHA256SUMS.asc",
            tmp_path / "topo-release-public.asc",
        )
        is False
    )

    output = capsys.readouterr().out
    assert "gpg tool not found" in output
    assert "Refusing unverified update" in output


@patch("src.manage.update.shutil.which", return_value="/usr/bin/gpg")
@patch("src.manage.update.subprocess.run")
def test_verify_release_signature_checks_pinned_key_and_signature(mock_run, _mock_which, tmp_path):
    sha256sums = tmp_path / "SHA256SUMS"
    signature = tmp_path / "SHA256SUMS.asc"
    public_key = tmp_path / "topo-release-public.asc"

    mock_run.side_effect = [
        MagicMock(returncode=0, stderr=""),
        MagicMock(
            returncode=0,
            stdout=(
                f"[GNUPG:] VALIDSIG {TOPO_RELEASE_KEY_FINGERPRINT} "
                f"2026-06-09 1781000000 0 4 0 1 10 00 {TOPO_RELEASE_KEY_FINGERPRINT}\n"
            ),
            stderr="gpg: Good signature",
        ),
    ]

    assert _verify_release_signature(sha256sums, signature, public_key) is True

    assert mock_run.call_count == 2
    assert "--import" in mock_run.call_args_list[0].args[0]
    assert "--status-fd=1" in mock_run.call_args_list[1].args[0]
    assert "--verify" in mock_run.call_args_list[1].args[0]


@patch("src.manage.update.shutil.which", return_value="/usr/bin/gpg")
@patch("src.manage.update.subprocess.run")
def test_verify_release_signature_rejects_unexpected_key(mock_run, _mock_which, tmp_path):
    attacker_fingerprint = "A" * 40
    mock_run.side_effect = [
        MagicMock(returncode=0, stderr=""),
        MagicMock(
            returncode=0,
            stdout=(
                f"[GNUPG:] VALIDSIG {attacker_fingerprint} "
                f"2026-06-09 1781000000 0 4 0 1 10 00 {attacker_fingerprint}\n"
            ),
            stderr="gpg: Good signature",
        ),
    ]

    assert (
        _verify_release_signature(
            tmp_path / "SHA256SUMS",
            tmp_path / "SHA256SUMS.asc",
            tmp_path / "topo-release-public.asc",
        )
        is False
    )

    assert mock_run.call_count == 2


@patch("src.manage.update.shutil.which", return_value="/usr/bin/gpg")
@patch("src.manage.update.subprocess.run")
def test_verify_release_signature_rejects_foreign_signature_even_if_topo_key_is_imported(
    mock_run, _mock_which, tmp_path
):
    attacker_fingerprint = "A" * 40
    mock_run.side_effect = [
        MagicMock(returncode=0, stderr="gpg: imported Topo and attacker public keys"),
        MagicMock(
            returncode=0,
            stdout=(
                f"[GNUPG:] VALIDSIG {attacker_fingerprint} "
                f"2026-06-09 1781000000 0 4 0 1 10 00 {attacker_fingerprint}\n"
            ),
            stderr="gpg: Good signature from attacker",
        ),
    ]

    assert (
        _verify_release_signature(
            tmp_path / "SHA256SUMS",
            tmp_path / "SHA256SUMS.asc",
            tmp_path / "topo-release-public.asc",
        )
        is False
    )

    assert mock_run.call_count == 2


@patch("src.manage.update.get_install_source", return_value="package")
@patch("src.manage.update.TOPO_VERSION", "0.9.1")
@patch("src.manage.update._fetch_latest_release_tag", return_value="v0.9.3")
@patch("src.manage.update.get_package_asset_name", return_value="topo-0.9.3-1.x86_64.rpm")
@patch("src.manage.update.get_package_upgrade_argv")
@patch("src.manage.update.subprocess.run")
def test_run_update_downloads_and_installs_package_update(
    mock_run,
    mock_upgrade_argv,
    _mock_asset_name,
    _mock_remote_tag,
    _mock_install_source,
    monkeypatch,
    capsys,
):
    package_bytes = b"rpm package"
    monkeypatch.setattr("src.manage.update._verify_release_signature", lambda *a, **kw: True)

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

    assert run_update() is True

    output = capsys.readouterr().out
    assert "New package available: v0.9.3" in output
    assert "Verified SHA256 for topo-0.9.3-1.x86_64.rpm" in output
    mock_run.assert_called_once()
    argv = mock_run.call_args.args[0]
    assert argv[:4] == ["sudo", "dnf", "upgrade", "-y"]
    assert argv[4].endswith("topo-0.9.3-1.x86_64.rpm")


@patch("src.manage.update.get_install_source", return_value="package")
@patch("src.manage.update.TOPO_VERSION", "0.9.1")
@patch("src.manage.update._fetch_latest_release_tag", return_value="v0.9.3")
@patch("src.manage.update.get_package_asset_name", return_value="topo-0.9.3-1.x86_64.rpm")
@patch("src.manage.update.get_package_upgrade_argv")
@patch("src.manage.update._verify_release_signature", return_value=True)
@patch("src.manage.update.subprocess.run")
def test_run_update_downloads_signature_assets_when_gpg_is_available(
    mock_run,
    _mock_verify_signature,
    mock_upgrade_argv,
    _mock_asset_name,
    _mock_remote_tag,
    _mock_install_source,
    monkeypatch,
):
    package_bytes = b"rpm package"
    downloaded = []
    monkeypatch.setattr(
        "src.manage.update.shutil.which", lambda name: "/usr/bin/gpg" if name == "gpg" else None
    )

    def fake_download(_url, destination, timeout=60):
        downloaded.append(destination.name)
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

    assert run_update() is True

    assert downloaded == [
        "topo-0.9.3-1.x86_64.rpm",
        "SHA256SUMS",
        "SHA256SUMS.asc",
        "topo-release-public.asc",
    ]


@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_does_not_install_when_remote_is_older(mock_check_output, mock_run):
    mock_check_output.return_value = '{"tag_name": "v0.0.1"}'

    assert run_update() is True

    mock_run.assert_not_called()


@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_does_not_install_when_remote_version_is_invalid(mock_check_output, mock_run):
    mock_check_output.return_value = '{"tag_name": "latest"}'

    assert run_update() is False

    mock_run.assert_not_called()


@patch("src.manage.update._verify_release_signature", return_value=True)
@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_installs_only_when_remote_is_newer(mock_check_output, mock_run, _mock_verify):
    # 1st check_output fetches release tag; 2nd downloads install.sh.
    script_content = "#!/usr/bin/env bash\n"
    script_sha = sha256(script_content.encode()).hexdigest()
    sums_content = f"{script_sha}  install.sh\n"

    def fake_download(url, destination, timeout=60):
        if destination.name == "SHA256SUMS":
            destination.write_text(sums_content)
        else:
            destination.write_bytes(b"sig")

    with patch("src.manage.update._download_file", fake_download):
        mock_check_output.side_effect = ['{"tag_name": "v999.0.0"}', script_content.encode("utf-8")]
        mock_run.return_value = MagicMock(returncode=0)

        assert run_update() is True

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

    assert run_update() is False

    mock_run.assert_not_called()


@patch("src.manage.update._verify_release_signature", return_value=True)
@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_rejects_non_script_payload(mock_check_output, mock_run, _mock_verify):
    non_script = "<html><body>503 Service Unavailable</body></html>"
    non_script_sha = sha256(non_script.encode()).hexdigest()
    sums_content = f"{non_script_sha}  install.sh\n"

    def fake_download(url, destination, timeout=60):
        if destination.name == "SHA256SUMS":
            destination.write_text(sums_content)
        else:
            destination.write_bytes(b"sig")

    with patch("src.manage.update._download_file", fake_download):
        mock_check_output.side_effect = [
            '{"tag_name": "v999.0.0"}',
            non_script.encode("utf-8"),
        ]

        assert run_update() is False

    mock_run.assert_not_called()


def test_update_helpers_handle_invalid_inputs(tmp_path):
    assert _parse_version("v1.2.3") == Version("1.2.3")
    assert _parse_version("not-a-version") is None
    assert _normalized_gpg_fingerprint("a" * 40) == "A" * 40
    assert _normalized_gpg_fingerprint("short") is None
    assert _subprocess_stderr_tail(SimpleNamespace(stderr="first\nlast\n")) == "last"
    assert _subprocess_stderr_tail(SimpleNamespace(stderr=None)) == ""
    sums = tmp_path / "SHA256SUMS"
    sums.write_text("bad line\nabc *other.rpm\n" + "A" * 64 + "  topo.rpm\n")
    assert _expected_sha256(sums, "topo.rpm") == "a" * 64
    assert _expected_sha256(sums, "missing") is None


def test_download_file_handles_os_error_and_retries(tmp_path):
    destination = tmp_path / "asset"
    with (
        patch("src.manage.update.subprocess.run", side_effect=OSError("network")),
        pytest.raises(OSError),
    ):
        _download_file("https://example.test/asset", destination, attempts=1)


@patch("src.manage.update.shutil.which", return_value="/usr/bin/gpg")
@patch("src.manage.update.subprocess.run")
def test_verify_signature_import_and_verify_failures(mock_run, _which, tmp_path, capsys):
    mock_run.return_value = MagicMock(returncode=1, stderr="bad key")
    assert not _verify_release_signature(tmp_path / "sums", tmp_path / "sig", tmp_path / "key")
    mock_run.reset_mock()
    mock_run.side_effect = [MagicMock(returncode=0, stderr=""), OSError("gpg")]
    assert not _verify_release_signature(tmp_path / "sums", tmp_path / "sig", tmp_path / "key")
    assert "Failed to verify" in capsys.readouterr().out


def test_run_update_invalid_local_and_fetch_failure(capsys):
    with (
        patch("src.manage.update.TOPO_VERSION", "bad"),
        patch("src.manage.update._fetch_latest_release_tag", return_value="v2.0.0"),
    ):
        assert run_update() is False
    assert "Invalid local version" in capsys.readouterr().out
    with (
        patch("src.manage.update.TOPO_VERSION", "1.0.0"),
        patch("src.manage.update._fetch_latest_release_tag", side_effect=OSError("offline")),
    ):
        assert run_update() is False
    assert "Failed to check latest release" in capsys.readouterr().out


def test_run_update_separates_a_missing_curl_from_a_failed_lookup(capsys):
    # _fetch_latest_release_tag() swallows every curl failure and returns "",
    # which used to surface as `Invalid release tag: ''` -- blaming the release
    # for a missing tool or a dropped network.
    with (
        patch("src.manage.update.TOPO_VERSION", "1.0.0"),
        patch("src.manage.update._fetch_latest_release_tag", return_value=""),
        patch("src.manage.update.shutil.which", return_value=None),
    ):
        assert run_update() is False
    output = capsys.readouterr().out
    assert "curl not found" in output
    assert "Invalid release tag" not in output

    with (
        patch("src.manage.update.TOPO_VERSION", "1.0.0"),
        patch("src.manage.update._fetch_latest_release_tag", return_value=""),
        patch("src.manage.update.shutil.which", return_value="/usr/bin/curl"),
    ):
        assert run_update() is False
    output = capsys.readouterr().out
    assert "Could not determine the latest release version" in output
    assert "Invalid release tag" not in output

    # A non-empty tag that does not parse is still reported as a bad tag.
    with (
        patch("src.manage.update.TOPO_VERSION", "1.0.0"),
        patch("src.manage.update._fetch_latest_release_tag", return_value="nightly"),
    ):
        assert run_update() is False
    assert "Invalid release tag" in capsys.readouterr().out


def test_run_update_package_unsupported_and_upgrade_failures(tmp_path, capsys):
    with patch("src.manage.update.get_package_asset_name", return_value=None):
        _run_package_update("1.0.0", "v2.0.0")
    assert "No Topo package for this distribution or architecture" in capsys.readouterr().out
    with (
        patch("src.manage.update.get_package_asset_name", return_value="topo.rpm"),
        patch("src.manage.update._download_file") as download,
        patch("src.manage.update._verify_release_signature", return_value=True),
        patch(
            "src.manage.update.get_package_upgrade_argv", return_value=["sudo", "dnf", "upgrade"]
        ),
        patch("src.manage.update.subprocess.run", return_value=MagicMock(returncode=2)),
        patch("src.manage.update.shutil.which", return_value=None),
    ):

        def fake_download(_url, destination, timeout=60):
            if destination.name == "SHA256SUMS":
                destination.write_text("0" * 64 + "  topo.rpm\n")
            else:
                destination.write_bytes(b"x")

        download.side_effect = fake_download
        _run_package_update("1.0.0", "v2.0.0")
    assert "Checksum mismatch" in capsys.readouterr().out
