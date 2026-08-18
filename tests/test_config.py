from src.core.config import (
    DEFAULT_CONFIG,
    load_config,
    normalize_config,
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


def test_load_config_returns_independent_defaults(test_env):
    config = load_config()
    config["theme_color"] = "mutated"

    assert DEFAULT_CONFIG["theme_color"] == "cyan"
    assert load_config()["theme_color"] == "cyan"


def test_normalize_config_rejects_invalid_types():
    config = normalize_config(
        {
            "use_trash": "yes",
            "min_age_days": -1,
            "show_scrollbar": "no",
            "theme_color": "",
        }
    )

    assert config == DEFAULT_CONFIG


def test_normalize_config_accepts_valid_values():
    config = normalize_config(
        {
            "use_trash": False,
            "min_age_days": 3,
            "show_scrollbar": False,
            "theme_color": "magenta",
        }
    )

    assert config["use_trash"] is False
    assert config["min_age_days"] == 3
    assert config["show_scrollbar"] is False
    assert config["theme_color"] == "magenta"
