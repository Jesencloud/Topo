from unittest.mock import MagicMock, patch

from src.clean.dev import clean_docker, clean_tool_cache


def test_clean_tool_cache_dry_run():
    """Verify tool cache cleanup logic in dry-run mode."""
    # This function uses run_command which we should patch
    with patch("src.clean.dev.run_command") as mock_run:
        size, items = clean_tool_cache("dummy tool", ["dummy", "clean"], dry_run=True)
        assert size == 0
        assert items == 1
        assert not mock_run.called


@patch("shutil.which")
@patch("src.clean.dev.run_command")
@patch("subprocess.run")
def test_clean_docker_execution(mock_sub_run, mock_run_cmd, mock_which):
    """Verify docker cleanup logic and sudo detection."""
    mock_which.return_value = "/usr/bin/docker"

    # Mock 'docker info' failing to trigger sudo
    mock_sub_run.return_value = MagicMock(returncode=1)

    clean_docker(dry_run=False)

    # Should call docker system prune with use_sudo=True
    mock_run_cmd.assert_called_with(
        ["docker", "system", "prune", "-f", "--volumes"], use_sudo=True, capture=True
    )
