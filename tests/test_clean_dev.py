from unittest.mock import MagicMock, patch

from src.clean.dev import (
    clean_ai_models,
    clean_developer_tools,
    clean_docker,
    clean_multipass,
    clean_podman,
    clean_tool_cache,
)
from src.core.heavy_cache import get_ai_model_cleanup_defs


def test_clean_tool_cache_dry_run():
    """Verify tool cache cleanup logic in dry-run mode."""
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
    mock_run_cmd.side_effect = [
        MagicMock(returncode=1, ok=False),
        MagicMock(returncode=0, ok=True),
    ]

    size, items = clean_docker(dry_run=False)
    assert items == 1
    mock_run_cmd.assert_called_with(
        ["docker", "system", "prune", "-f", "--volumes"], use_sudo=True, capture=True
    )


@patch("shutil.which")
@patch("src.clean.dev.run_command")
def test_clean_podman(mock_run_cmd, mock_which, test_env):
    mock_which.return_value = "/usr/bin/podman"
    mock_run_cmd.return_value = MagicMock(returncode=0)

    # Ensure the cache path exists for the test
    cache_path = test_env / ".cache/containers"
    cache_path.mkdir(parents=True, exist_ok=True)

    with (
        patch("src.clean.dev.clean_path_by_age", return_value=(100, 1)),
        patch("pathlib.Path.exists", return_value=True),
    ):
        size, items = clean_podman(dry_run=False)
        assert items == 2  # 1 for prune, 1 for cache
        mock_run_cmd.assert_called_with(["podman", "system", "prune", "-f"], capture=True)


@patch("shutil.which")
@patch("src.clean.dev.run_command")
def test_clean_multipass(mock_run_cmd, mock_which):
    mock_which.return_value = "/usr/bin/multipass"
    mock_run_cmd.return_value = MagicMock(returncode=0)
    size, items = clean_multipass(dry_run=False)
    assert items == 1
    mock_run_cmd.assert_called_with(["multipass", "purge"], capture=True)


@patch("src.clean.dev.clean_path_by_age")
def test_clean_ai_models(mock_clean_age):
    mock_clean_age.return_value = (500, 2)
    size, items = clean_ai_models(dry_run=True)
    assert size > 0
    assert items > 0


@patch("src.clean.dev.clean_path_by_age")
def test_clean_ai_models_uses_shared_cleanup_defs(mock_clean_age, test_env):
    mock_clean_age.return_value = (0, 0)

    clean_ai_models(dry_run=True)

    calls_by_path = {call.args[0]: call.kwargs["days"] for call in mock_clean_age.call_args_list}
    for definition in get_ai_model_cleanup_defs():
        assert calls_by_path[definition.resolved_path()] == definition.age_days


@patch("shutil.which")
@patch("src.clean.dev.clean_tool_cache")
def test_clean_developer_tools(mock_clean_tool, mock_which):
    mock_which.return_value = "/usr/bin/npm"  # Mock npm presence
    mock_clean_tool.return_value = (100, 1)

    with (
        patch("src.clean.dev.get_size_fast", return_value=2048),
        patch("pathlib.Path.exists", return_value=True),
        patch("src.clean.dev.safe_remove", return_value=(True, "deleted")),
    ):
        size, items, cats = clean_developer_tools(dry_run=False)
        assert cats > 0
        assert size > 0


def test_clean_tool_cache_reports_actual_freed(test_env):
    """Freed space should be (before - after), not the pre-clean size."""
    cache = test_env / ".cache/mytool"
    cache.mkdir(parents=True)
    with (
        patch("src.clean.dev.run_command", return_value=MagicMock(returncode=0)),
        patch("src.clean.dev.get_size_fast", side_effect=[1000, 200]),  # before, after
        patch("pathlib.Path.exists", return_value=True),
    ):
        size, items = clean_tool_cache("mytool", ["mytool", "clean"], str(cache), dry_run=False)
    assert items == 1
    assert size == 800  # 1000 - 200, not the pre-clean 1000
