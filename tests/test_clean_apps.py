from pathlib import Path
from unittest.mock import MagicMock, patch

from src.clean.apps import (
    clean_app_generic,
    clean_generic_xdg_caches,
    clean_orphaned_remnants,
    clean_snap_cache,
    proactive_app_detection,
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
