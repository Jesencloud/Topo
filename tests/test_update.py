from unittest.mock import MagicMock, patch

from src.manage.update import _should_update, run_update


def test_should_update_uses_semantic_version_ordering():
    assert _should_update("1.9.0", "1.10.0") is True
    assert _should_update("1.10.0", "1.9.0") is False
    assert _should_update("1.10.0", "1.10.0") is False
    assert _should_update("1.10.0", "not-a-version") is False


@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_does_not_install_when_remote_is_older(mock_check_output, mock_run):
    mock_check_output.return_value = "0.0.1\n"

    run_update()

    mock_run.assert_not_called()


@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_does_not_install_when_remote_version_is_invalid(mock_check_output, mock_run):
    mock_check_output.return_value = "latest\n"

    run_update()

    mock_run.assert_not_called()


@patch("src.manage.update.subprocess.run")
@patch("src.manage.update.subprocess.check_output")
def test_run_update_installs_only_when_remote_is_newer(mock_check_output, mock_run):
    mock_check_output.return_value = "999.0.0\n"
    mock_run.return_value = MagicMock(returncode=0)

    run_update()

    mock_run.assert_called_once()
