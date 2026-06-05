from unittest.mock import MagicMock, patch

from src.manage.update import _should_update, run_update


def test_should_update_uses_semantic_version_ordering():
    assert _should_update("1.9.0", "1.10.0") is True
    assert _should_update("1.9.0", "v1.10.0") is True
    assert _should_update("1.10.0", "1.9.0") is False
    assert _should_update("1.10.0", "1.10.0") is False
    assert _should_update("1.10.0", "not-a-version") is False


@patch("src.manage.update.get_install_source", return_value="package")
@patch("src.manage.update.get_package_manager_commands", return_value=["sudo apt upgrade topo"])
@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_delegates_package_installs_to_package_manager(
    mock_check_output, mock_run, _mock_commands, _mock_install_source, capsys
):
    run_update()

    output = capsys.readouterr().out
    assert "sudo apt upgrade topo" in output
    assert "sudo dnf upgrade topo" not in output
    mock_check_output.assert_not_called()
    mock_run.assert_not_called()


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
