from pathlib import Path
from unittest.mock import MagicMock, call, patch

from src.clean.apps import (
    clean_app_generic,
    clean_apps_deep,
    clean_browser_caches,
    clean_generic_xdg_caches,
    clean_orphaned_remnants,
    clean_snap_cache,
    proactive_app_detection,
)
from src.core.app_cache import (
    find_cleanable_cache_dirs_in_roots,
    find_standard_cache_dirs,
    find_xdg_cache_candidates,
    get_cache_cleanable_reason,
    is_generic_xdg_cache_path,
)
from src.core.desktop_app_cache import (
    get_desktop_app_cleanup_defs,
    is_desktop_app_cache_path,
)
from src.core.file_ops import CACHEDIR_TAG_SIGNATURE


def test_proactive_app_detection():
    with (
        patch("src.clean.apps.DETECTED_APPS_FILE", Path("/tmp/nonexistent")),
        patch("pathlib.Path.exists", return_value=False),
    ):
        detected = proactive_app_detection()
        assert isinstance(detected, dict)


def test_proactive_app_detection_health_check(test_env):
    mock_registry = test_env / "detected_apps.json"
    mock_registry.write_text('{"dead_app": {"paths": ["/tmp/nonexistent"], "procs": ["dead_app"]}}')

    with (
        patch("src.clean.apps.DETECTED_APPS_FILE", mock_registry),
        patch("shutil.which", return_value=None),
        patch("pathlib.Path.exists", return_value=False),
        patch("pathlib.Path.iterdir", return_value=[]),
    ):
        detected = proactive_app_detection()
        assert "dead_app" not in detected


def test_proactive_app_detection_write_error(test_env):
    # Mock finding a new app but fail to write the registry
    with (
        patch("shutil.which", return_value="/usr/bin/new_app"),
        patch("pathlib.Path.iterdir") as mock_iter,
        patch("builtins.open", side_effect=OSError("Write failed")),
    ):
        m_dir = MagicMock()
        m_dir.is_dir.return_value = True
        m_dir.is_symlink.return_value = False
        m_dir.name = "new_app"
        mock_iter.return_value = [m_dir]
        detected = proactive_app_detection()
        assert "new_app" in detected


def test_proactive_app_detection_skips_symlinks(test_env):
    """Regression (M2): a symlink in ~/.cache must not be resolved into the
    cleanup registry, or its (out-of-tree) target's contents could be wiped."""
    real_data = test_env / "important-data"
    real_data.mkdir()
    link = test_env / ".cache" / "toolname"  # named like an installed command
    link.symlink_to(real_data)
    registry = test_env / "detected_apps.json"

    with (
        patch("src.clean.apps.DETECTED_APPS_FILE", registry),
        patch("shutil.which", return_value="/usr/bin/toolname"),
    ):
        detected = proactive_app_detection()

    assert "toolname" not in detected
    assert all(
        "important-data" not in p for info in detected.values() for p in info.get("paths", [])
    )


def test_clean_flatpak_unused():
    from src.clean.apps import clean_flatpak_unused

    with patch("shutil.which", return_value="/usr/bin/flatpak"):
        # Dry run
        size, items = clean_flatpak_unused(dry_run=True)
        assert size == 0
        assert items == 0

        # Real run
        with patch("src.clean.apps.run_command") as mock_run:
            mock_run.return_value = MagicMock(stdout="Uninstalling\nfreed 1 GB")
            size, items = clean_flatpak_unused(dry_run=False)
            assert items == 1
            assert size > 0


def test_clean_generic_xdg_caches(test_env):
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.clean_path_by_age", return_value=(100, 1)) as mock_clean_age,
    ):
        cache_dir = test_env / ".cache/dummy_cache"
        cache_dir.mkdir(parents=True)
        size, items = clean_generic_xdg_caches(dry_run=True)
        assert items >= 0
        mock_clean_age.assert_called_with(cache_dir, days=3, dry_run=True)


def test_find_xdg_cache_candidates_classifies_top_level_cache_dirs(test_env):
    obvious_cache = test_env / ".cache/build-cache"
    stale_app_cache = test_env / ".cache/randomapp"
    nested_cache = stale_app_cache / "nested-cache"
    for path in (obvious_cache, nested_cache):
        path.mkdir(parents=True)

    candidates = find_xdg_cache_candidates(days=30)
    by_path = {candidate.path: candidate for candidate in candidates}

    assert by_path[obvious_cache].age_days == 3
    assert by_path[obvious_cache].label == "Generic Cache"
    assert by_path[stale_app_cache].age_days == 30
    assert by_path[stale_app_cache].label == "Stale App Data"
    assert nested_cache not in by_path
    assert is_generic_xdg_cache_path(obvious_cache) is True
    assert is_generic_xdg_cache_path(test_env / ".cache") is False


def test_clean_generic_xdg_caches_skips_symlinked_cache_dirs(test_env):
    real_data = test_env / "important-cache-target"
    real_data.mkdir()
    marker = real_data / "data.bin"
    marker.write_bytes(b"keep")
    link = test_env / ".cache/link-cache"
    link.symlink_to(real_data)

    with patch("pathlib.Path.home", return_value=test_env):
        size, items = clean_generic_xdg_caches(days=0, dry_run=False)

    assert size == 0
    assert items == 0
    assert marker.exists()
    assert link.exists()
    assert is_generic_xdg_cache_path(link) is False


def test_clean_generic_xdg_caches_removes_cachedir_tagged_directory(test_env):
    cache_dir = test_env / ".cache/tagged-cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "CACHEDIR.TAG").write_text(f"{CACHEDIR_TAG_SIGNATURE}\n")
    (cache_dir / "data.bin").write_bytes(b"1" * 512)

    with patch("pathlib.Path.home", return_value=test_env):
        size, items = clean_generic_xdg_caches(dry_run=False)

    assert size >= 512
    assert items == 1
    assert not cache_dir.exists()


def test_clean_generic_xdg_caches_dry_run_keeps_cachedir_tagged_directory(test_env):
    cache_dir = test_env / ".cache/tagged-cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "CACHEDIR.TAG").write_text(f"{CACHEDIR_TAG_SIGNATURE}\n")
    (cache_dir / "data.bin").write_bytes(b"1" * 512)

    with patch("pathlib.Path.home", return_value=test_env):
        size, items = clean_generic_xdg_caches(dry_run=True)

    assert size >= 512
    assert items == 1
    assert cache_dir.exists()


def test_find_standard_cache_dirs_finds_tagged_dirs_and_prunes(test_env):
    cache_root = test_env / ".cache"
    tagged = cache_root / "tagged-cache"
    nested_under_tagged = tagged / "nested"
    nested_tagged = cache_root / "app" / "nested-tagged"
    tagged.mkdir(parents=True)
    nested_under_tagged.mkdir()
    nested_tagged.mkdir(parents=True)
    (tagged / "CACHEDIR.TAG").write_text(f"{CACHEDIR_TAG_SIGNATURE}\n")
    (nested_under_tagged / "CACHEDIR.TAG").write_text(f"{CACHEDIR_TAG_SIGNATURE}\n")
    (nested_tagged / "CACHEDIR.TAG").write_text(f"{CACHEDIR_TAG_SIGNATURE}\n")

    top_level = find_standard_cache_dirs(cache_root, max_depth=1)
    recursive = find_standard_cache_dirs(cache_root)

    assert top_level == [tagged]
    assert tagged in recursive
    assert nested_tagged in recursive
    assert nested_under_tagged not in recursive
    assert get_cache_cleanable_reason(tagged) == "CACHEDIR.TAG"


def test_find_standard_cache_dirs_skips_tagged_symlinked_dirs(test_env):
    target = test_env / "external-cache"
    target.mkdir()
    (target / "CACHEDIR.TAG").write_text(f"{CACHEDIR_TAG_SIGNATURE}\n")
    link = test_env / ".cache/linked-cache"
    link.symlink_to(target)

    paths = find_standard_cache_dirs(test_env / ".cache")

    assert link not in paths
    assert get_cache_cleanable_reason(link) == ""


def test_desktop_app_cache_defs_resolve_home_dynamically(test_env):
    defs = get_desktop_app_cleanup_defs()

    assert test_env / ".config/discord/Cache" in defs["Discord"]["paths"]
    assert is_desktop_app_cache_path(test_env / ".cache/spotify/Data") is True
    assert is_desktop_app_cache_path(test_env / "Documents/WeChat Files") is False


def test_clean_apps_deep_uses_desktop_app_cache_defs(test_env):
    discord_cache = test_env / ".config/discord/Cache"
    discord_cache.mkdir(parents=True)
    cache_file = discord_cache / "blob.bin"
    cache_file.write_bytes(b"d" * 256)

    with patch("src.clean.apps.is_app_running", return_value=False):
        size, items, categories = clean_apps_deep(dry_run=False, detected_apps={})

    assert size >= 256
    assert items >= 1
    assert categories >= 1
    assert discord_cache.exists()
    assert not cache_file.exists()


def test_find_cleanable_cache_dirs_in_roots_finds_browser_cache_children_only(test_env):
    chrome_profile = test_env / ".config/google-chrome/Default"
    cache_dir = chrome_profile / "Cache"
    cache_storage = chrome_profile / "Service Worker/CacheStorage"
    service_worker_db = chrome_profile / "Service Worker/Database"
    firefox_disk_cache = test_env / ".cache/mozilla/firefox/profile.default/cache2"
    cache_dir.mkdir(parents=True)
    cache_storage.mkdir(parents=True)
    service_worker_db.mkdir(parents=True)
    firefox_disk_cache.mkdir(parents=True)
    (chrome_profile / "Login Data").write_text("{}")

    paths = find_cleanable_cache_dirs_in_roots(
        [".config/google-chrome", ".cache/mozilla"], include_named_cache_dirs=True
    )

    assert cache_dir in paths
    assert cache_storage in paths
    assert firefox_disk_cache in paths
    assert chrome_profile not in paths
    assert chrome_profile / "Service Worker" not in paths
    assert service_worker_db not in paths


def test_find_cleanable_cache_dirs_in_roots_finds_desktop_app_cache_children(test_env):
    slack_root = test_env / ".config/Slack"
    cache_dir = slack_root / "Cache"
    cache_storage = slack_root / "Service Worker/CacheStorage"
    service_worker_db = slack_root / "Service Worker/Database"
    cache_dir.mkdir(parents=True)
    cache_storage.mkdir(parents=True)
    service_worker_db.mkdir(parents=True)
    (slack_root / "storage.json").write_text("{}")

    paths = find_cleanable_cache_dirs_in_roots(
        [".config/Slack"], require_sensitive_app_data_root=True
    )

    assert cache_dir in paths
    assert cache_storage in paths
    assert slack_root not in paths
    assert slack_root / "Service Worker" not in paths
    assert service_worker_db not in paths


def test_clean_browser_caches_removes_known_browser_cache_children(test_env):
    chrome_profile = test_env / ".config/google-chrome/Default"
    chrome_cache = chrome_profile / "Cache"
    chrome_cache.mkdir(parents=True)
    chrome_cache_file = chrome_cache / "data.bin"
    chrome_cache_file.write_bytes(b"c" * 256)
    chrome_login_db = chrome_profile / "Login Data"
    chrome_login_db.write_text("{}")

    firefox_profile = test_env / ".mozilla/firefox/profile.default"
    firefox_cache = test_env / ".cache/mozilla/firefox/profile.default/cache2"
    firefox_startup_cache = firefox_profile / "startupCache"
    firefox_cache.mkdir(parents=True)
    firefox_startup_cache.mkdir(parents=True)
    firefox_cache_file = firefox_cache / "entry.bin"
    firefox_startup_file = firefox_startup_cache / "startup.bin"
    firefox_cache_file.write_bytes(b"f" * 128)
    firefox_startup_file.write_bytes(b"s" * 128)
    firefox_login_db = firefox_profile / "logins.json"
    firefox_login_db.write_text("{}")

    with patch("src.clean.apps.is_app_running", return_value=False):
        size, items, categories = clean_browser_caches(dry_run=False)

    assert size >= 512
    assert items == 3
    assert categories == 2
    assert chrome_cache.exists()
    assert firefox_cache.exists()
    assert firefox_startup_cache.exists()
    assert not chrome_cache_file.exists()
    assert not firefox_cache_file.exists()
    assert not firefox_startup_file.exists()
    assert chrome_login_db.exists()
    assert firefox_login_db.exists()


def test_clean_browser_caches_skips_running_browser(test_env):
    firefox_profile = test_env / ".mozilla/firefox/profile.default"
    firefox_cache = firefox_profile / "cache2"
    firefox_cache.mkdir(parents=True)
    cache_file = firefox_cache / "entry.bin"
    cache_file.write_bytes(b"f" * 128)

    with (
        patch(
            "src.clean.apps.BROWSER_CACHE_DEFS",
            {"Firefox": {"roots": [".mozilla"], "procs": ["firefox"]}},
        ),
        patch("src.clean.apps.is_app_running", return_value=True),
    ):
        size, items, categories = clean_browser_caches(dry_run=False)

    assert size == 0
    assert items == 0
    assert categories == 0
    assert cache_file.exists()


def test_clean_orphaned_remnants(test_env):
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.clean_path_by_age", return_value=(100, 1)),
        patch("shutil.which", return_value=None),
    ):
        config_dir = test_env / ".config/orphan_app"
        config_dir.mkdir(parents=True)
        size, items = clean_orphaned_remnants(dry_run=True)
        assert items >= 0


def test_clean_app_generic_dry_run(test_env):
    """Verify that dry_run calculates size but doesn't delete."""
    # Setup dummy cache
    app_cache_dir = test_env / ".config/myapp/Cache"
    app_cache_dir.mkdir(parents=True)
    (app_cache_dir / "data.bin").write_bytes(b"0" * 2048)  # 2KB

    # Path variants in clean_app_generic uses Path.expanduser()
    # In test_env, HOME is redirected to temp dir.
    paths = [str(app_cache_dir)]

    # Run in dry_run mode
    freed, items = clean_app_generic("MyApp", paths, dry_run=True)

    assert freed == 2048
    assert items == 1
    assert app_cache_dir.exists()
    assert (app_cache_dir / "data.bin").exists()


@patch("src.clean.apps.is_app_running")
def test_clean_app_generic_skips_when_running(mock_is_running, test_env):
    """Verify that cleanup is skipped if the app is currently running."""
    mock_is_running.return_value = True

    app_cache_dir = test_env / ".config/myapp/Cache"
    app_cache_dir.mkdir(parents=True)

    freed, items = clean_app_generic("MyApp", [str(app_cache_dir)], process_names=["myapp"])

    assert freed == 0
    assert items == 0
    assert mock_is_running.called


def test_clean_app_generic_execution(test_env):
    """Verify that actual execution deletes the files."""
    app_cache_dir = test_env / ".config/myapp/Cache"
    app_cache_dir.mkdir(parents=True)
    (app_cache_dir / "data.bin").write_bytes(b"0" * 100)

    # We pass the parent dir, clean_app_generic cleans its *contents*
    freed, items = clean_app_generic("MyApp", [str(app_cache_dir)], dry_run=False)

    assert items == 1
    assert app_cache_dir.exists()
    assert not (app_cache_dir / "data.bin").exists()


def test_clean_app_generic_reuses_fast_size_for_safe_remove(test_env):
    app_cache_dir = test_env / ".config/myapp/Cache"
    app_cache_dir.mkdir(parents=True)
    cache_file = app_cache_dir / "data.bin"
    cache_file.write_bytes(b"0" * 100)

    with (
        patch("src.clean.apps.get_size_fast", return_value=100) as mock_size,
        patch("src.clean.apps.safe_remove", return_value=(True, "deleted")) as mock_remove,
    ):
        freed, items = clean_app_generic("MyApp", [str(app_cache_dir)], dry_run=False)

    assert freed == 100
    assert items == 1
    mock_size.assert_has_calls([call(cache_file)])
    mock_remove.assert_called_once_with(cache_file, use_trash=False, known_size_bytes=100)


def test_clean_app_generic_keeps_protected_desktop_config(test_env):
    dconf_dir = test_env / ".config/dconf"
    dconf_dir.mkdir(parents=True)
    settings_file = dconf_dir / "user"
    settings_file.write_bytes(b"gnome settings")

    freed, items = clean_app_generic("dconf", [str(dconf_dir)], dry_run=False)

    assert freed == 0
    assert items == 0
    assert settings_file.exists()


def test_clean_snap_cache(test_env):
    """Verify that clean_snap_cache identifies and cleans snap caches."""
    snap_dir = test_env / "snap/spotify/common/.cache"
    snap_dir.mkdir(parents=True)
    (snap_dir / "data.bin").write_bytes(b"0" * 1024)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.clean_path_by_age", return_value=(1024, 1)),
    ):
        size, items = clean_snap_cache(dry_run=True)
        assert size == 1024
        assert items == 1
