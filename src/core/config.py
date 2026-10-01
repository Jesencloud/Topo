import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

from .constants import WARN
from .json_store import ensure_private_dir, read_json, write_json_atomic
from .paths import get_config_dir


def get_config_file() -> Path:
    return get_config_dir() / "config.json"


# Bumped whenever a key starts being honoured, so load_config() can tell a
# deliberate user choice from a value that no released version ever read.
CONFIG_VERSION = 2

# Named title colors, resolved to escapes by constants._resolve_theme_title().
# Keeping the accepted set here (rather than accepting any string) means an
# unknown name is rejected at load time instead of blanking the title.
THEME_COLOR_NAMES = ("purple", "cyan", "blue", "magenta", "green", "yellow", "red")

# Upper bound on min_age_days. It is a floor on file age, so 100 years is already
# far past any real use -- but the real job is to reject the absurd: age_cutoff()
# computes ``time.time() - min_age_days * SECONDS_PER_DAY``, and a hand-edited
# value of a few hundred digits (JSON admits any-precision ints, and the only
# other check is ``>= 0``) overflows the int->float conversion and crashes every
# age-gated cleanup with an uncaught OverflowError. A value over the cap is
# treated like any other invalid one: dropped back to the default.
MAX_MIN_AGE_DAYS = 36500

DEFAULT_CONFIG: dict[str, Any] = {
    "config_version": CONFIG_VERSION,
    "use_trash": True,
    # A floor, not a per-task threshold: every cleaner keeps its own window and
    # this can only push it further into the past. 0 means "no floor", i.e. the
    # shipped default changes nothing -- which matters because some sweeps
    # deliberately have no age gate at all (a container transfer cache, a snap's
    # ~/.cache), and a non-zero default would quietly start sparing files there.
    "min_age_days": 0,
    "show_scrollbar": True,
    # The color topo has always drawn its titles in. Changing this key is the
    # only thing that moves it.
    "theme_color": "purple",
}

# Keys that were written to config.json by <= 1.1.2 but read by nobody.
_LEGACY_INERT_KEYS = ("min_age_days", "theme_color")

_config_cache: dict[str, Any] | None = None

_CONFIG_WARNING_EMITTED = False


def _stored_config_version(user_config: dict[str, Any]) -> int:
    """The version a stored config claims, or 1 for the pre-version files.

    <= 1.1.2 wrote no config_version key at all, so a missing one means "the
    oldest format". A value that is not a plain int (or is a bool, which is an
    int subclass) is treated the same way: it is not a version this code ever
    wrote, so migration should run and re-stamp it rather than trust it.
    """
    stored = user_config.get("config_version")
    if isinstance(stored, bool) or not isinstance(stored, int):
        return 1
    return stored


def _migrate_config(user_config: dict[str, Any]) -> dict[str, Any]:
    """Upgrade a stored config in place from its version toward CONFIG_VERSION.

    Each step is guarded by the version it starts from, so the next format bump
    (v2 -> v3) adds a branch here and actually runs. That is the whole reason the
    caller gates on "version is behind" rather than "no config_version key": the
    absence-only gate could fire exactly once ever, so a v2 -> v3 migration could
    never join the v1 -> v2 one below -- a stored v2 file would slip straight
    through normalize_config with no migration step applied.
    """
    if _stored_config_version(user_config) < 2:
        # 1 -> 2: min_age_days and theme_color were written by <= 1.1.2 but read
        # by nobody, so a stored value was never a deliberate choice. Drop it
        # rather than suddenly honour it -- otherwise wiring the keys up would
        # move every existing install off the cleanup thresholds and the title
        # color it has always had.
        for key in _LEGACY_INERT_KEYS:
            user_config.pop(key, None)
    return user_config


def _warn_config_unreadable() -> None:
    """Say once, on stderr, that config.json exists but could not be read.

    The fallback defaults are the safer side for every key but ``min_age_days``:
    that one is a floor the user raised to make topo *more* cautious than its own
    defaults, and the default is 0 (no floor). Falling back to it silently would
    drop the floor and let a cleanup delete files it was sparing -- the same
    worst-direction, silent failure the whitelist refuses to make. Announcing it
    (the whitelist warns for the same reason) is what keeps that from happening
    with nothing on screen to notice it by. A *missing* config is a fresh install
    and stays silent; only a present-but-unreadable one is announced.
    """
    global _CONFIG_WARNING_EMITTED
    if _CONFIG_WARNING_EMITTED:
        return
    _CONFIG_WARNING_EMITTED = True
    print(
        f"{WARN} topo: cannot read {get_config_file()} -- using default settings "
        "this run. Any min_age_days floor you set is NOT in effect, so a cleanup "
        "may remove files it would otherwise spare. Fix the file, or delete it to "
        "start from the defaults.",
        file=sys.stderr,
    )


def load_config() -> dict[str, Any]:
    """Read config.json, or the defaults when it is absent or unreadable.

    Reading deliberately does not create the file. Every command now reads the
    config (the title color is resolved before the first line of output), and a
    read that wrote would mean `topo remove` created ~/.config/topo/config.json
    at startup and then reported it as leftover configuration it had removed.

    Most of the defaults are the *safer* side (``use_trash`` on), so an absent
    config -- a fresh install -- falls back silently. ``min_age_days`` is the one
    exception: it is a floor the user raised to make topo more cautious, and its
    default is 0 (no floor), so falling back drops that floor and *widens* what a
    cleanup deletes. A present-but-unreadable config is therefore announced on
    stderr (via _warn_config_unreadable), the same way the whitelist refuses to
    lose a protection in silence; a missing one is not. The defaults are still
    used either way, because there is nothing trustworthy to use instead. What
    must not happen is writing those defaults back over a file that merely failed
    to parse, so the save below is reached only from the legacy-migration branch,
    which by then has a file it could read.
    """
    user_config, state = read_json(get_config_file())
    if state != "ok":
        if state == "unreadable":
            _warn_config_unreadable()
        return deepcopy(DEFAULT_CONFIG)

    if isinstance(user_config, dict) and _stored_config_version(user_config) < CONFIG_VERSION:
        # A config an older topo wrote: migrate it up its version gradient, then
        # normalize_config (which carries config_version from DEFAULT_CONFIG)
        # stamps the current version, and save it once so the upgrade sticks. A
        # file already at CONFIG_VERSION -- or ahead of it, which a downgrade must
        # not clobber -- skips straight to normalize_config below without a save.
        config = normalize_config(_migrate_config(user_config))
        save_config(config)
        return config

    return normalize_config(user_config)


def normalize_config(user_config: Any) -> dict[str, Any]:
    config = deepcopy(DEFAULT_CONFIG)
    if not isinstance(user_config, dict):
        return config

    min_age_days = user_config.get("min_age_days")
    if (
        isinstance(min_age_days, int)
        and not isinstance(min_age_days, bool)
        and 0 <= min_age_days <= MAX_MIN_AGE_DAYS
    ):
        config["min_age_days"] = min_age_days

    for key in ("use_trash", "show_scrollbar"):
        value = user_config.get(key)
        if isinstance(value, bool):
            config[key] = value

    theme_color = user_config.get("theme_color")
    if isinstance(theme_color, str) and theme_color.lower() in THEME_COLOR_NAMES:
        config["theme_color"] = theme_color.lower()

    return config


def save_config(config: dict[str, Any]) -> bool:
    if not ensure_private_dir(get_config_dir()):
        return False
    if not write_json_atomic(get_config_file(), config, mode=0o600):
        return False
    clear_config_cache()
    return True


def clear_config_cache() -> None:
    """Drop the memoized config so the next read picks the file up again."""
    global _config_cache
    _config_cache = None


def get_config() -> dict[str, Any]:
    """load_config() memoized for the life of the process.

    The deletion loops ask for ``use_trash`` and the age floor once per
    candidate -- tens of thousands of times in one `topo clean` -- and every
    load_config() call re-reads and re-parses the file. Nothing but save_config()
    changes it mid-run, and that drops the cache, so a hand edit between runs
    still takes effect on the next one.

    The snapshot is per process: this cache fills once and is held for the
    process's life, and clear_config_cache() only fires in the process that
    called save_config(). A *different* process editing the config while this
    one runs is not observed here until this process next starts -- a deliberate
    semantic, not an oversight. Making it cross-process would put a stat() on the
    config file into every read to compare mtimes, and this cache exists
    precisely because that read sits on the per-candidate hot path; the
    single-instance lock already keeps a destructive command from overlapping a
    config write for more than the moment before it acquires.

    The result always carries every key and a validated value, because
    load_config() runs the file through normalize_config() -- which is why the
    getters below can index it and coerce, with no second round of checks.
    """
    global _config_cache
    if _config_cache is None:
        _config_cache = load_config()
    return _config_cache


def get_show_scrollbar() -> bool:
    return bool(get_config()["show_scrollbar"])


def get_use_trash() -> bool:
    """Whether a recoverable deletion goes to the trash instead of being wiped.

    Only consulted where the data is worth recovering -- app residue, backup
    files, the directories `topo analyze` deletes on request. Caches and stale
    temp files ignore it and are always deleted outright: moving a 4 GiB cache
    to ~/.local/share/Trash frees nothing, which is the one thing a cleanup tool
    must not pretend to have done.
    """
    return bool(get_config()["use_trash"])


def get_min_age_days() -> int:
    """The floor, in days, under which nothing is old enough to be cleaned.

    Cleaners keep their own thresholds (30 days for caches, 7 for editor
    backups, 3 for /tmp, and none at all for a couple of pure-cache sweeps);
    this raises any that sit below it. It cannot lower one, so no config edit can
    make a cleaner more aggressive than the code is. The default 0 leaves every
    threshold exactly where the code put it.
    """
    return int(get_config()["min_age_days"])


def get_theme_color() -> str:
    """Name of the title color; see THEME_COLOR_NAMES."""
    return str(get_config()["theme_color"])
