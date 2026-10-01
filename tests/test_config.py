import json
import os
import stat

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


def test_an_unparsable_config_falls_back_without_being_overwritten(test_env, monkeypatch, capsys):
    # A present-but-unreadable config falls back to defaults, which drops any
    # min_age_days floor the user set -- a cleanup would silently widen -- so it
    # warns (the whitelist does the same). The file the user still has to fix must
    # survive untouched.
    monkeypatch.setattr("src.core.config._CONFIG_WARNING_EMITTED", False)
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.json"
    config_file.write_text('{"use_trash": fal')
    clear_config_cache()

    assert load_config() == DEFAULT_CONFIG
    warning = capsys.readouterr().err
    assert str(config_file) in warning
    assert "min_age_days" in warning
    assert config_file.read_text() == '{"use_trash": fal'


def test_a_missing_config_falls_back_without_warning(test_env, monkeypatch, capsys):
    # A fresh install -- no file at all -- is the one case the defaults are the
    # whole truth, so it must stay silent (unlike the unreadable case above).
    monkeypatch.setattr("src.core.config._CONFIG_WARNING_EMITTED", False)
    clear_config_cache()

    assert load_config() == DEFAULT_CONFIG
    assert capsys.readouterr().err == ""


def test_the_unreadable_config_warning_is_printed_once(test_env, monkeypatch, capsys):
    monkeypatch.setattr("src.core.config._CONFIG_WARNING_EMITTED", False)
    config_file = get_config_dir() / "config.json"
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text("{not json")

    for _ in range(5):
        clear_config_cache()
        load_config()

    assert capsys.readouterr().err.count("cannot read") == 1


def test_a_config_whose_bytes_are_not_utf8_falls_back(test_env, monkeypatch, capsys):
    # A theme name written by an editor in latin-1 used to raise
    # UnicodeDecodeError before the first line of any command's output. Now the
    # stray byte decodes to U+FFFD, the JSON still parses (state "ok"), and only
    # the unknown theme name falls back via normalize_config -- so no warning: the
    # file was readable, nothing the user set was silently dropped but the color.
    monkeypatch.setattr("src.core.config._CONFIG_WARNING_EMITTED", False)
    config_dir = get_config_dir()
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.json").write_bytes(b'{"config_version": 2, "theme_color": "caf\xe9"}')
    clear_config_cache()

    assert load_config()["theme_color"] == DEFAULT_CONFIG["theme_color"]
    assert capsys.readouterr().err == ""


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


def test_save_config_writes_a_private_file_and_directory(test_env):
    # The config dir holds the whitelist too, so both the dir and the file are
    # brought to owner-only. umask 000 proves it is the explicit mode (and the
    # dir chmod) that tightens them, not a lucky inherited umask.
    old = os.umask(0o000)
    try:
        assert save_config(dict(DEFAULT_CONFIG)) is True
    finally:
        os.umask(old)

    config_dir = get_config_dir()
    assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE((config_dir / "config.json").stat().st_mode) == 0o600


def test_a_config_behind_the_current_version_is_migrated_and_restamped(test_env, monkeypatch):
    # The bug P2-2 fixed: the gate keyed on a *missing* config_version, so once a
    # file was stamped it could never migrate again -- a v2 -> v3 step could never
    # join the v1 -> v2 one. Simulate a future bump and confirm a now-behind file
    # is migrated, re-stamped and saved, with its real choices preserved.
    future = CONFIG_VERSION + 1
    monkeypatch.setattr("src.core.config.CONFIG_VERSION", future)
    monkeypatch.setattr(
        "src.core.config.DEFAULT_CONFIG", {**DEFAULT_CONFIG, "config_version": future}
    )
    config_file = _write_raw_config({"config_version": CONFIG_VERSION, "use_trash": False})

    config = load_config()

    assert config["config_version"] == future
    assert config["use_trash"] is False
    assert json.loads(config_file.read_text())["config_version"] == future


def test_a_config_at_the_current_version_is_not_rewritten(test_env):
    # No migration is due, so load_config must not re-save: the on-disk bytes stay
    # exactly as written (save_config would reformat them with indent=4).
    config_file = _write_raw_config({"config_version": CONFIG_VERSION, "theme_color": "cyan"})
    before = config_file.read_text()

    load_config()

    assert config_file.read_text() == before
