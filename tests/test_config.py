from src.core.config import add_purge_path, load_config, remove_purge_path, save_config


def test_config_lifecycle(test_env):
    """Verify that config is correctly saved and loaded from the temp HOME."""
    config = load_config()
    assert config["theme_color"] == "cyan"  # Default

    config["theme_color"] = "magenta"
    save_config(config)

    new_config = load_config()
    assert new_config["theme_color"] == "magenta"


def test_purge_paths_management(test_env):
    """Verify adding and removing custom search paths for Purge mode."""
    test_path = test_env / "CustomProjects"
    test_path.mkdir()

    # Add
    assert add_purge_path(str(test_path)) is True
    assert str(test_path.resolve()) in load_config()["purge_search_paths"]

    # Add duplicate (should be False)
    assert add_purge_path(str(test_path)) is False

    # Remove
    assert remove_purge_path(str(test_path)) is True
    assert str(test_path.resolve()) not in load_config()["purge_search_paths"]

    # Remove non-existent
    assert remove_purge_path("/non/existent/path") is False
