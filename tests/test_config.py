from src.core.config import (
    DEFAULT_CONFIG,
    add_purge_path,
    load_config,
    normalize_config,
    remove_purge_path,
    save_config,
)


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


def test_load_config_returns_independent_defaults(test_env):
    config = load_config()
    config["purge_search_paths"].append("/tmp/mutated")

    assert "/tmp/mutated" not in DEFAULT_CONFIG["purge_search_paths"]
    assert "/tmp/mutated" not in load_config()["purge_search_paths"]


def test_normalize_config_rejects_invalid_types():
    config = normalize_config(
        {
            "purge_search_paths": "not-a-list",
            "use_trash": "yes",
            "min_age_days": -1,
            "status_public_ip": "false",
            "theme_color": "",
        }
    )

    assert config == DEFAULT_CONFIG


def test_normalize_config_accepts_valid_values():
    config = normalize_config(
        {
            "purge_search_paths": ["/tmp/projects"],
            "use_trash": False,
            "min_age_days": 3,
            "status_public_ip": True,
            "theme_color": "magenta",
        }
    )

    assert config["purge_search_paths"] == ["/tmp/projects"]
    assert config["use_trash"] is False
    assert config["min_age_days"] == 3
    assert config["status_public_ip"] is True
    assert config["theme_color"] == "magenta"
