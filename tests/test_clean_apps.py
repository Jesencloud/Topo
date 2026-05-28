from unittest.mock import patch

from src.clean.apps import clean_app_generic


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
