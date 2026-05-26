import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from src.clean.user import clean_trash, clean_user_caches

def test_clean_trash_dry_run(test_env):
    """Verify trash cleanup in dry-run mode (should only report size)."""
    trash_dir = test_env / ".local/share/Trash/files"
    trash_dir.mkdir(parents=True)
    (trash_dir / "junk.txt").write_text("garbage")
    
    # size of 'garbage' is 7 bytes
    size, items, cats = clean_trash(dry_run=True)
    
    assert size == 7
    assert items == 1
    assert (trash_dir / "junk.txt").exists()

@patch("shutil.which")
@patch("subprocess.run")
def test_clean_trash_execution_gio(mock_run, mock_which, test_env):
    """Verify trash cleanup using 'gio' command."""
    mock_which.side_effect = lambda x: "/usr/bin/gio" if x == "gio" else None
    
    # Create a dummy file to ensure total_cleaned > 0
    trash_dir = test_env / ".local/share/Trash/files"
    trash_dir.mkdir(parents=True, exist_ok=True)
    (trash_dir / "test.txt").write_text("content")
    
    # We don't care about the return size here as much as the command call
    clean_trash(dry_run=False)
    
    mock_run.assert_called_with(["gio", "trash", "--empty"], capture_output=True)

def test_clean_user_caches_dry_run(test_env):
    """Verify user cache cleanup dry-run."""
    cache_dir = test_env / ".cache/thumbnails"
    cache_dir.mkdir(parents=True)
    (cache_dir / "thumb.png").write_bytes(b"0" * 500)
    
    # This should find the file and report 500 bytes
    size, items, cats = clean_user_caches(dry_run=True)
    
    assert size >= 500
    assert items >= 1
    assert (cache_dir / "thumb.png").exists()
