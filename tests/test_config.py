import json

from src.core.config import (
    CONFIG_VERSION,
    DEFAULT_CONFIG,
    clear_config_cache,
    get_config,
    get_min_age_days,
    get_theme_color,
    get_use_trash,
    load_config,
    normalize_config,
    save_config,
)
from src.core.paths import get_config_dir


def _write_raw_config(payload):
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps(payload))
    clear_config_cache()
    return config_file


def test_config_lifecycle(test_env):
    """Verify that config is correctly saved and loaded from the temp HOME."""
    config = load_config()
    assert config["theme_color"] == "purple"  # Default

    config["theme_color"] = "magenta"
    save_config(config)

    new_config = load_config()
    assert new_config["theme_color"] == "magenta"


def test_load_config_returns_independent_defaults(test_env):
    config = load_config()
    config["theme_color"] = "mutated"

    assert DEFAULT_CONFIG["theme_color"] == "purple"
    assert load_config()["theme_color"] == "purple"


def test_reading_the_config_does_not_create_the_file(test_env):
    # Every command resolves the title color before printing anything, so a read
    # that wrote would have `topo remove` create ~/.config/topo/config.json at
    # startup and then report it as leftover configuration.
    load_config()

    assert not (get_config_dir() / "config.json").exists()


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


def test_normalize_config_rejects_a_bool_as_min_age_days():
    # bool is a subclass of int, so `True` would otherwise pass the type check
    # and become a one-day floor.
    assert (
        normalize_config({"min_age_days": True})["min_age_days"] == DEFAULT_CONFIG["min_age_days"]
    )


def test_normalize_config_rejects_an_unknown_theme_color():
    assert normalize_config({"theme_color": "chartreuse"})["theme_color"] == "purple"


def test_normalize_config_accepts_valid_values():
    config = normalize_config(
        {
            "use_trash": False,
            "min_age_days": 10,
            "show_scrollbar": False,
            "theme_color": "MAGENTA",
        }
    )

    assert config["use_trash"] is False
    assert config["min_age_days"] == 10
    assert config["show_scrollbar"] is False
    assert config["theme_color"] == "magenta"


def test_legacy_config_drops_the_keys_that_never_did_anything(test_env):
    # A file with no config_version was written when min_age_days and
    # theme_color were inert, so the stored values cannot be deliberate: they
    # are dropped rather than suddenly honoured, which would move every existing
    # install off the thresholds and the title color it has always had.
    config_file = _write_raw_config({"use_trash": False, "min_age_days": 90, "theme_color": "red"})

    config = load_config()

    assert config["min_age_days"] == DEFAULT_CONFIG["min_age_days"]
    assert config["theme_color"] == "purple"
    # use_trash was honoured before, so it survives.
    assert config["use_trash"] is False
    # Rewritten and stamped once, so a choice made from now on sticks.
    stored = json.loads(config_file.read_text())
    assert stored["config_version"] == CONFIG_VERSION
    assert stored["use_trash"] is False


def test_stamped_config_keeps_the_values_it_stores(test_env):
    _write_raw_config({"config_version": CONFIG_VERSION, "min_age_days": 45, "theme_color": "cyan"})

    config = load_config()

    assert config["min_age_days"] == 45
    assert config["theme_color"] == "cyan"


def test_get_config_is_memoized_until_the_cache_is_cleared(test_env):
    assert get_use_trash() is True

    _write_raw_config({"config_version": CONFIG_VERSION, "use_trash": False})
    # _write_raw_config clears the cache, so the new value is visible...
    assert get_use_trash() is False

    # ...and a later edit is not, until something drops the cache: the deletion
    # loops ask per candidate, and re-reading the file each time would cost a
    # stat and a parse tens of thousands of times per run.
    (get_config_dir() / "config.json").write_text(
        json.dumps({"config_version": CONFIG_VERSION, "use_trash": True})
    )
    assert get_use_trash() is False

    clear_config_cache()
    assert get_use_trash() is True


def test_save_config_drops_the_cache(test_env):
    assert get_theme_color() == "purple"

    config = get_config()
    config["theme_color"] = "green"
    save_config(config)

    assert get_theme_color() == "green"


def test_get_min_age_days_falls_back_on_a_corrupt_value(test_env):
    # A string would break the age arithmetic (max(days, "soon")), so it never
    # reaches a getter: normalize_config drops it on the way in.
    _write_raw_config({"config_version": CONFIG_VERSION, "min_age_days": "soon"})

    assert get_min_age_days() == DEFAULT_CONFIG["min_age_days"]


def test_an_unparsable_config_falls_back_without_being_overwritten(test_env):
    # Unlike the whitelist, defaulting here is the safe direction (use_trash on),
    # so it stays quiet -- but the file the user still has to fix must survive.
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.json"
    config_file.write_text('{"use_trash": fal')
    clear_config_cache()

    assert load_config() == DEFAULT_CONFIG
    assert config_file.read_text() == '{"use_trash": fal'


def test_a_config_whose_bytes_are_not_utf8_falls_back(test_env):
    # A theme name written by an editor in latin-1 used to raise
    # UnicodeDecodeError before the first line of any command's output.
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_bytes(b'{"config_version": 2, "theme_color": "caf\xe9"}')
    clear_config_cache()

    assert load_config()["theme_color"] == DEFAULT_CONFIG["theme_color"]


def test_saving_the_config_leaves_no_scratch_file_behind(test_env):
    save_config(load_config())

    assert sorted(p.name for p in get_config_dir().iterdir()) == ["config.json"]


def test_a_failed_save_keeps_the_stored_config(test_env, monkeypatch):
    _write_raw_config({"config_version": CONFIG_VERSION, "theme_color": "cyan"})
    config_file = get_config_dir() / "config.json"

    def dump_then_die(data, fp, **kwargs):
        fp.write("{")
        raise OSError("No space left on device")

    monkeypatch.setattr(json, "dump", dump_then_die)
    assert save_config({"config_version": CONFIG_VERSION, "theme_color": "green"}) is False

    monkeypatch.undo()
    clear_config_cache()
    assert json.loads(config_file.read_text())["theme_color"] == "cyan"
    assert load_config()["theme_color"] == "cyan"
