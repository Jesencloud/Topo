from unittest.mock import patch, MagicMock
from pathlib import Path
from src.clean.apps import clean_app_generic, proactive_app_detection, clean_generic_xdg_caches, clean_orphaned_remnants

def test_proactive_app_detection():
    with patch("src.clean.apps.DETECTED_APPS_FILE", Path("/tmp/nonexistent")):
        with patch("pathlib.Path.exists", return_value=False):
            detected = proactive_app_detection()
            assert isinstance(detected, dict)

def test_proactive_app_detection_health_check(test_env):
    mock_registry = test_env / "detected_apps.json"
    mock_registry.write_text('{"dead_app": {"paths": ["/tmp/nonexistent"], "procs": ["dead_app"]}}')
    
    with patch("src.clean.apps.DETECTED_APPS_FILE", mock_registry):
        with patch("shutil.which", return_value=None):
            with patch("pathlib.Path.exists", return_value=False):
                with patch("pathlib.Path.iterdir", return_value=[]):
                    detected = proactive_app_detection()
                    assert "dead_app" not in detected

def test_proactive_app_detection_write_error(test_env):
    # Mock finding a new app but fail to write the registry
    with patch("shutil.which", return_value="/usr/bin/new_app"):
        with patch("pathlib.Path.iterdir") as mock_iter:
            m_dir = MagicMock()
            m_dir.is_dir.return_value = True
            m_dir.name = "new_app"
            mock_iter.return_value = [m_dir]
            with patch("builtins.open", side_effect=Exception("Write failed")):
                detected = proactive_app_detection()
                assert "new_app" in detected

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
    with patch("pathlib.Path.home", return_value=test_env):
        with patch("src.clean.apps.clean_path_by_age", return_value=(100, 1)):
            cache_dir = test_env / ".cache/dummy_cache"
            cache_dir.mkdir(parents=True)
            size, items = clean_generic_xdg_caches(dry_run=True)
            assert items >= 0

def test_clean_orphaned_remnants(test_env):
    with patch("pathlib.Path.home", return_value=test_env):
        with patch("src.clean.apps.clean_path_by_age", return_value=(100, 1)):
            config_dir = test_env / ".config/orphan_app"
            config_dir.mkdir(parents=True)
            with patch("shutil.which", return_value=None):
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
