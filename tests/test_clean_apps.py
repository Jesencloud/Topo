import json
import time
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from src.clean.apps import (
    clean_app_generic,
    clean_apps_deep,
    clean_browser_caches,
    clean_desktop_apps_caches,
    clean_generic_xdg_caches,
    clean_ide_caches,
    clean_orphaned_remnants,
    clean_snap_cache,
    clean_steam_shader_cache,
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
from src.core.system import CommandResult


def _no_real_package_tooling():
    """Stop ``clean_apps_deep`` from running the one privileged command it owns.

    The call runs the whole cleaner registry, and ``clean_flatpak_unused`` is in
    it: left alone it really executes ``sudo flatpak uninstall --system``, which
    both waits for a password (sudo reads it from /dev/tty, which pytest does
    not capture) and, once given one, uninstalls this machine's system runtimes.
    ``apps.py`` has a single ``run_command`` call site, so patching it names
    that command and nothing else.
    """
    return patch(
        "src.clean.apps.run_command",
        return_value=CommandResult(args=["flatpak"], returncode=1),
    )


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


def test_an_unreadable_app_registry_is_rebuilt_instead_of_crashing(test_env):
    # This file is derived data, so the safe answer is to rebuild it -- but the
    # old reader could not get that far: json.load() on bytes that are not UTF-8
    # raises UnicodeDecodeError, a ValueError that slipped past
    # `except (OSError, JSONDecodeError)` and turned `topo clean` into a traceback.
    registry = test_env / "detected_apps.json"
    registry.write_bytes(b'{"caf\xe9": {"paths": []}')

    with (
        patch("src.clean.apps.DETECTED_APPS_FILE", registry),
        patch("shutil.which", return_value=None),
    ):
        detected = proactive_app_detection()

    assert detected == {}
    # Rewritten, so the next run does not re-read the same unusable file.
    assert json.loads(registry.read_text()) == {}


def test_an_app_registry_holding_the_wrong_shape_is_not_trusted(test_env):
    # A JSON list parses fine and then explodes on .items().
    registry = test_env / "detected_apps.json"
    registry.write_text(json.dumps(["firefox"]))

    with (
        patch("src.clean.apps.DETECTED_APPS_FILE", registry),
        patch("shutil.which", return_value=None),
    ):
        assert proactive_app_detection() == {}


def test_an_app_entry_that_is_not_a_dict_is_dropped(test_env):
    # One level further down: the top-level dict is fine, and then info.get()
    # raises AttributeError on the entry someone hand-edited to a bare number.
    registry = test_env / "detected_apps.json"
    registry.write_text(json.dumps({"firefox": 3}))

    with (
        patch("src.clean.apps.DETECTED_APPS_FILE", registry),
        patch("shutil.which", return_value=None),
    ):
        assert proactive_app_detection() == {}

    # And rewritten, so the same unusable entry is not re-read on every run.
    assert json.loads(registry.read_text()) == {}


def test_a_failed_registry_write_keeps_the_previous_one(test_env):
    registry = test_env / "detected_apps.json"
    registry.write_text(json.dumps({"olddata": {"paths": [str(test_env)], "procs": ["olddata"]}}))

    def dump_then_die(data, fp, **kwargs):
        fp.write("{")
        raise OSError("No space left on device")

    with (
        patch("src.clean.apps.DETECTED_APPS_FILE", registry),
        patch("shutil.which", return_value="/usr/bin/newapp"),
        patch("json.dump", dump_then_die),
        patch("pathlib.Path.iterdir") as mock_iter,
    ):
        found = MagicMock()
        found.is_dir.return_value = True
        found.is_symlink.return_value = False
        found.name = "newapp"
        mock_iter.return_value = [found]
        proactive_app_detection()

    assert json.loads(registry.read_text())["olddata"]["procs"] == ["olddata"]
    assert list(test_env.glob("detected_apps.json.tmp-*")) == []


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
    assert test_env / ".var/app/com.tencent.WeChat/cache" in defs["WeChat"]["paths"]
    assert test_env / ".var/app/com.tencent.WeChat/config/xwechat" not in defs["WeChat"]["paths"]
    assert test_env / ".xwechat" not in defs["WeChat"]["paths"]
    assert test_env / "Documents/WeChat Files" not in defs["WeChat"]["paths"]
    assert is_desktop_app_cache_path(test_env / ".cache/spotify/Data") is True
    assert is_desktop_app_cache_path(test_env / "Documents/WeChat Files") is False


def test_desktop_app_cache_paths_reuse_resolved_definitions(test_env):
    from src.core.desktop_app_cache import _resolved_desktop_app_cache_paths

    _resolved_desktop_app_cache_paths.cache_clear()
    assert is_desktop_app_cache_path(test_env / ".cache/spotify/Data") is True
    assert is_desktop_app_cache_path(test_env / ".config/discord/Cache/data") is True
    info = _resolved_desktop_app_cache_paths.cache_info()
    assert info.misses == 1
    assert info.hits == 1


def test_clean_apps_deep_keeps_wechat_user_data(test_env):
    wechat_cache = test_env / ".var/app/com.tencent.WeChat/cache"
    wechat_cache.mkdir(parents=True)
    cache_file = wechat_cache / "cache.bin"
    cache_file.write_bytes(b"cache")

    flatpak_user_data = test_env / ".var/app/com.tencent.WeChat/config/xwechat"
    legacy_user_data = test_env / ".xwechat"
    documents_data = test_env / "Documents/WeChat Files"
    for path in (flatpak_user_data, legacy_user_data, documents_data):
        path.mkdir(parents=True)
        (path / "message.db").write_text("keep")

    with patch("src.clean.apps.is_app_running", return_value=False), _no_real_package_tooling():
        size, items, categories = clean_apps_deep(dry_run=False, detected_apps={})

    assert size >= len(b"cache")
    assert items >= 1
    assert categories >= 1
    assert wechat_cache.exists()
    assert not cache_file.exists()
    assert (flatpak_user_data / "message.db").exists()
    assert (legacy_user_data / "message.db").exists()
    assert (documents_data / "message.db").exists()


def test_clean_apps_deep_uses_desktop_app_cache_defs(test_env):
    discord_cache = test_env / ".config/discord/Cache"
    discord_cache.mkdir(parents=True)
    cache_file = discord_cache / "blob.bin"
    cache_file.write_bytes(b"d" * 256)

    with patch("src.clean.apps.is_app_running", return_value=False), _no_real_package_tooling():
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


def test_find_cleanable_cache_dirs_in_roots_skips_symlinked_named_cache_dirs(test_env):
    external = test_env / "valuable"
    external.mkdir()
    link = test_env / ".config/google-chrome/Default/Cache"
    link.parent.mkdir(parents=True)
    link.symlink_to(external, target_is_directory=True)

    paths = find_cleanable_cache_dirs_in_roots(
        [".config/google-chrome"], include_named_cache_dirs=True
    )

    assert link not in paths
    assert external not in paths


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


def test_clean_browser_caches_does_not_follow_symlinked_named_cache_dir(test_env):
    external = test_env / "valuable"
    external.mkdir()
    important_file = external / "important.txt"
    important_file.write_text("keep")

    link = test_env / ".config/google-chrome/Default/Cache"
    link.parent.mkdir(parents=True)
    link.symlink_to(external, target_is_directory=True)

    with patch("src.clean.apps.is_app_running", return_value=False):
        size, items, categories = clean_browser_caches(dry_run=False)

    assert size == 0
    assert items == 0
    assert categories == 0
    assert important_file.exists()
    assert link.is_symlink()


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


def test_clean_app_generic_does_not_follow_symlinked_cache_root(test_env):
    external = test_env / "valuable"
    external.mkdir()
    important_file = external / "important.txt"
    important_file.write_text("keep")

    link = test_env / ".cache/app-cache"
    link.symlink_to(external, target_is_directory=True)

    freed, items = clean_app_generic("App Cache", [str(link)], dry_run=False)

    assert freed == 0
    assert items == 0
    assert important_file.exists()
    assert link.is_symlink()


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


def test_clean_app_generic_reuses_parent_fast_scan_for_child_sizes(test_env):
    app_cache_dir = test_env / ".config/myapp/Cache"
    app_cache_dir.mkdir(parents=True)
    cache_file_a = app_cache_dir / "a.bin"
    cache_file_b = app_cache_dir / "b.bin"
    cache_file_a.write_bytes(b"0" * 100)
    cache_file_b.write_bytes(b"0" * 200)

    with (
        patch(
            "src.core.file_ops.get_rust_scan_data",
            return_value={"total_size_bytes": 300, "subdirs": {"a.bin": 100, "b.bin": 200}},
        ) as mock_scan,
        patch("src.clean.apps.get_size_fast") as mock_size,
        patch("src.clean.apps.safe_remove", return_value=(True, "deleted")) as mock_remove,
    ):
        freed, items = clean_app_generic("MyApp", [str(app_cache_dir)], dry_run=False)

    assert freed == 300
    assert items == 2
    mock_scan.assert_called_once_with(app_cache_dir.resolve())
    mock_size.assert_not_called()
    assert call(cache_file_a, use_trash=False, known_size_bytes=100) in mock_remove.call_args_list
    assert call(cache_file_b, use_trash=False, known_size_bytes=200) in mock_remove.call_args_list


def test_clean_app_generic_falls_back_when_parent_fast_scan_unavailable(test_env):
    app_cache_dir = test_env / ".config/myapp/Cache"
    app_cache_dir.mkdir(parents=True)
    cache_file = app_cache_dir / "data.bin"
    cache_file.write_bytes(b"0" * 100)

    with (
        patch("src.core.file_ops.get_rust_scan_data", return_value=None),
        patch("src.clean.apps.get_size_fast", return_value=100) as mock_size,
        patch("src.clean.apps.safe_remove", return_value=(True, "deleted")) as mock_remove,
    ):
        freed, items = clean_app_generic("MyApp", [str(app_cache_dir)], dry_run=False)

    assert freed == 100
    assert items == 1
    mock_size.assert_called_once_with(cache_file)
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


def test_flatpak_unused_paths(capsys):
    from src.clean.apps import clean_flatpak_unused

    with patch("src.clean.apps.shutil.which", return_value=None):
        assert clean_flatpak_unused() == (0, 0)
    with patch("src.clean.apps.shutil.which", return_value="/usr/bin/flatpak"):
        assert clean_flatpak_unused(dry_run=True) == (0, 0)
    with (
        patch("src.clean.apps.shutil.which", return_value="/usr/bin/flatpak"),
        patch("src.clean.apps.Path.is_dir", return_value=False),
        patch(
            "src.clean.apps.run_command",
            return_value=MagicMock(ok=True, stdout="Uninstalling 10 MB"),
        ),
    ):
        assert clean_flatpak_unused() == (10 * 1024 * 1024, 1)


def test_flatpak_unused_also_sweeps_the_system_installation():
    """Debian and Ubuntu users follow Flathub's system-wide setup.

    The unused runtimes then sit in /var/lib/flatpak, which only root can clear,
    so a --user-only pass reported success while reclaiming nothing.
    """
    from src.clean.apps import clean_flatpak_unused

    with (
        patch("src.clean.apps.shutil.which", return_value="/usr/bin/flatpak"),
        patch("src.clean.apps.Path.is_dir", return_value=True),
        patch(
            "src.clean.apps.run_command",
            return_value=MagicMock(ok=True, stdout="Uninstalling 4 MB"),
        ) as run,
    ):
        assert clean_flatpak_unused() == (8 * 1024 * 1024, 1)

    scopes = [(call.args[0][2], call.kwargs["use_sudo"]) for call in run.call_args_list]
    assert scopes == [("--user", False), ("--system", True)]


def test_flatpak_unused_skips_the_system_pass_without_a_system_install():
    """No /var/lib/flatpak means no reason to ask for a sudo password."""
    from src.clean.apps import clean_flatpak_unused

    with (
        patch("src.clean.apps.shutil.which", return_value="/usr/bin/flatpak"),
        patch("src.clean.apps.Path.is_dir", return_value=False),
        patch(
            "src.clean.apps.run_command",
            return_value=MagicMock(ok=True, stdout="nothing unused to uninstall"),
        ) as run,
    ):
        assert clean_flatpak_unused() == (0, 0)

    assert [call.kwargs["use_sudo"] for call in run.call_args_list] == [False]


def test_clean_app_generic_file_and_failure(test_env):
    path = test_env / "cache.bin"
    path.write_bytes(b"abc")
    with patch("src.clean.apps.safe_remove", return_value=(True, "ok")):
        assert clean_app_generic("App Cache", [str(path)]) == (3, 1)
    path.write_bytes(b"abc")
    with patch("src.clean.apps.safe_remove", return_value=(False, "denied")):
        assert clean_app_generic("App Cache", [str(path)]) == (0, 0)


def test_orphaned_remnants_removes_old_cache(test_env):
    import os

    cache = test_env / ".cache/orphan-app"
    cache.mkdir(parents=True)
    old = time.time() - 100 * 86400
    os.utime(cache, (old, old))
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.shutil.which", return_value=None),
        patch("src.clean.apps.get_size_fast", return_value=20),
        patch("src.clean.apps.safe_remove", return_value=(True, "ok")),
    ):
        assert clean_orphaned_remnants() == (20, 1)


def test_snap_shader_ide_and_desktop_cleaners(test_env):
    snap_cache = test_env / "snap/app/common/.cache"
    snap_cache.mkdir(parents=True)
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.is_app_running", return_value=True),
    ):
        assert clean_snap_cache() == (0, 0)
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.clean_path_by_age", return_value=(5, 1)),
        patch("src.clean.apps.is_app_running", return_value=False),
    ):
        assert clean_snap_cache() == (5, 1)

    shader = test_env / ".cache/mesa_shader_cache"
    shader.mkdir(parents=True)
    ide = test_env / ".config/Code/Cache"
    ide.mkdir(parents=True)
    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.clean_path_by_age", return_value=(4, 1)),
    ):
        assert clean_steam_shader_cache()[0] > 0
        assert clean_ide_caches()[0] > 0


def test_clean_desktop_and_deep_aggregation():
    with (
        patch("src.clean.apps.get_desktop_app_cleanup_defs", return_value={}),
        patch("src.clean.apps.clean_app_generic", return_value=(3, 2)),
    ):
        assert clean_desktop_apps_caches({"demo": {"paths": ["/tmp/demo"]}}) == (3, 2)
    with (
        patch("src.clean.apps.clean_desktop_apps_caches", return_value=(1, 2)),
        patch(
            "src.clean.apps.AppCleanerRegistry.cleaners",
            [lambda dry_run=False: (3, 4), lambda dry_run=False: (5, 6, 2)],
        ),
    ):
        assert clean_apps_deep(detected_apps={}) == (9, 12, 4)


def test_clean_snap_cache_covers_the_revision_data_directory(test_env):
    """$SNAP_USER_DATA holds a cache too, not just $SNAP_USER_COMMON.

    "current" is a symlink to the installed revision, which is where snaps that
    honour XDG_CACHE_HOME put their cache; sweeping only common/.cache missed it.
    """
    (test_env / "snap/app/common/.cache").mkdir(parents=True)
    (test_env / "snap/app/x1/.cache").mkdir(parents=True)
    (test_env / "snap/app/current").symlink_to(test_env / "snap/app/x1")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.is_app_running", return_value=False),
        patch("src.clean.apps.clean_path_by_age", return_value=(5, 1)) as by_age,
    ):
        assert clean_snap_cache() == (10, 2)

    swept = sorted(str(c.args[0]).replace(str(test_env), "") for c in by_age.call_args_list)
    assert swept == ["/snap/app/common/.cache", "/snap/app/current/.cache"]


def test_clean_steam_shader_cache_covers_sandboxed_steam(test_env):
    """A flatpak or snap Steam relocates the shader cache, the prefixes and HOME."""
    flatpak_home = test_env / ".var/app/com.valvesoftware.Steam"
    snap_home = test_env / "snap/steam/common"
    (flatpak_home / ".local/share/Steam/shadercache").mkdir(parents=True)
    (snap_home / ".local/share/Steam/shadercache").mkdir(parents=True)
    (flatpak_home / "cache/mesa_shader_cache").mkdir(parents=True)
    (snap_home / ".nv/GLCache").mkdir(parents=True)
    prefix_temp = flatpak_home / ".local/share/Steam/steamapps/compatdata/570/pfx"
    (prefix_temp / "drive_c/windows/temp").mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.clean_path_by_age", return_value=(4, 1)) as by_age,
    ):
        assert clean_steam_shader_cache() == (20, 5)

    swept = {str(c.args[0]).replace(str(test_env), "") for c in by_age.call_args_list}
    assert swept == {
        "/.var/app/com.valvesoftware.Steam/.local/share/Steam/shadercache",
        "/snap/steam/common/.local/share/Steam/shadercache",
        "/.var/app/com.valvesoftware.Steam/cache/mesa_shader_cache",
        "/snap/steam/common/.nv/GLCache",
        (
            "/.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/compatdata"
            "/570/pfx/drive_c/windows/temp"
        ),
    }


def test_clean_steam_shader_cache_counts_a_symlinked_steam_root_once(test_env):
    """~/.steam/steam is normally a symlink to ~/.local/share/Steam."""
    real_root = test_env / ".local/share/Steam"
    (real_root / "shadercache").mkdir(parents=True)
    (real_root / "steamapps/compatdata/620/pfx/drive_c/windows/temp").mkdir(parents=True)
    (test_env / ".steam").mkdir(parents=True)
    (test_env / ".steam/steam").symlink_to(real_root)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.clean_path_by_age", return_value=(4, 1)) as by_age,
    ):
        assert clean_steam_shader_cache() == (8, 2)

    assert len(by_age.call_args_list) == 2


def test_clean_ide_caches_covers_the_flatpak_install(test_env):
    """Flatpak redirects XDG_CONFIG_HOME, so ~/.config/Code matches nothing there."""
    flatpak_code = test_env / ".var/app/com.visualstudio.code/config/Code"
    (flatpak_code / "CachedData").mkdir(parents=True)
    (flatpak_code / "Cache").mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.clean_path_by_age", return_value=(4, 1)) as by_age,
    ):
        assert clean_ide_caches() == (8, 2)

    swept = sorted(str(c.args[0]).replace(str(test_env), "") for c in by_age.call_args_list)
    assert swept == [
        "/.var/app/com.visualstudio.code/config/Code/Cache",
        "/.var/app/com.visualstudio.code/config/Code/CachedData",
    ]


def test_orphaned_remnants_keeps_caches_of_packaged_apps(test_env):
    """A --user flatpak exports its launcher under home, not /var/lib/flatpak.

    Missing that directory is not harmless: the app is installed, but its cache
    folder looks orphaned and gets trashed. The stem of a packaged entry is also
    never the cache folder's name -- flatpak exports "<app.id>.desktop" and snapd
    generates "<snap>_<app>.desktop" -- so those components have to be indexed
    too or the lookup misses every packaged app.
    """
    import os

    old = time.time() - 100 * 86400
    for name in ("keepme", "snappy"):
        cache = test_env / ".cache" / name
        cache.mkdir(parents=True)
        os.utime(cache, (old, old))
    binary = test_env / "flatpak-run"
    binary.write_text("#!/bin/sh\n")

    exports = test_env / ".local/share/flatpak/exports/share/applications"
    exports.mkdir(parents=True)
    (exports / "com.example.Keepme.desktop").write_text(f"[Desktop Entry]\nExec={binary} %U\n")
    local = test_env / ".local/share/applications"
    local.mkdir(parents=True)
    (local / "snappy_snappy.desktop").write_text(f"[Desktop Entry]\nExec={binary} %U\n")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.clean.apps.shutil.which", return_value=None),
        patch("src.clean.apps.get_size_fast", return_value=20),
        patch("src.clean.apps.safe_remove", return_value=(True, "ok")) as remove,
    ):
        assert clean_orphaned_remnants() == (0, 0)

    remove.assert_not_called()
