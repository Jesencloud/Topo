"""The shared browser location table, and the rule that it stays the only copy."""

from pathlib import Path

from src.core.browser_paths import (
    BROWSER_CACHE_DEFS,
    BROWSER_DEFS,
    BROWSER_PROFILE_PATHS,
    BROWSER_PROFILE_TARGETS,
)


def _globs(name):
    return next(t.profile_globs for t in BROWSER_PROFILE_TARGETS if t.name == name)


def test_flatpak_profile_globs_derive_both_relocation_shapes():
    """Flatpak moves XDG_CONFIG_HOME and HOME to different places under the app.

    Deriving this is what removed the hand-written ".var/app/<id>/..." literal
    from every browser entry. Getting the ".mozilla" case wrong would silently
    drop a Flatpak Firefox rather than fail loudly.
    """
    assert ".var/app/org.chromium.Chromium/config/chromium/*" in _globs("Chromium")
    assert ".var/app/org.mozilla.firefox/.mozilla/firefox/*" in _globs("Firefox")


def test_profile_glob_defaults_to_the_root_itself():
    """Only Firefox and Brave nest their profiles below the root they declare."""
    assert ".zen/*" in _globs("Zen Browser")
    assert ".config/BraveSoftware/Brave-Browser/*" in _globs("Brave Browser")


def test_a_root_that_is_itself_the_profile_gets_no_wildcard():
    """opera://about reports ~/.config/opera as the profile, not its container."""
    assert ".config/opera" in _globs("Opera")
    assert ".config/opera/*" not in _globs("Opera")


def test_flatpak_relocation_is_applied_only_to_native_roots():
    """Snap already moved its roots; relocating them again names nothing real."""
    relocated_twice = [
        glob
        for target in BROWSER_PROFILE_TARGETS
        for glob in target.profile_globs
        if glob.startswith(".var/app/") and "snap/" in glob
    ]

    assert relocated_twice == []


def test_one_snap_declaration_reaches_all_three_consumers():
    """Protection, cache cleanup and database optimization read the same entry.

    Ubuntu ships Chromium only as a snap, so this path is that desktop's default
    layout rather than an alternative one -- and each consumer used to carry its
    own copy of it, or miss it entirely.
    """
    snap_profile = "snap/chromium/common/chromium"

    assert snap_profile in BROWSER_PROFILE_PATHS
    assert snap_profile in BROWSER_CACHE_DEFS["Chromium"]["roots"]
    assert f"{snap_profile}/*" in _globs("Chromium")


def test_every_browser_declares_a_known_database_engine():
    """A browser without an engine is invisible to database optimization."""
    for name, info in BROWSER_DEFS.items():
        assert info.get("engine") in {"gecko", "chromium"}, name


def test_no_other_module_repeats_a_browser_location_literal():
    """The point of the table is that it is the only place these paths exist.

    optimize.py held its own copy of the snap and Flatpak profile roots, so a
    browser could be protected from cleanup and still unreachable for vacuuming.
    """
    src = Path(__file__).parents[1] / "src"
    literals = ("snap/chromium/common", "snap/firefox/common", ".var/app/org.mozilla")
    offenders = [
        f"{path.relative_to(src)}:{literal}"
        for path in src.rglob("*.py")
        if path.name != "browser_paths.py"
        for literal in literals
        if literal in path.read_text(errors="ignore")
    ]

    assert offenders == []
