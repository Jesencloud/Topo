import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
from src.manage.uninstall import UninstallManager

def test_parse_size_to_bytes():
    mgr = UninstallManager()
    assert mgr._parse_size_to_bytes("1.5 GB") == 1500_000_000
    assert mgr._parse_size_to_bytes("500 MB") == 500_000_000
    assert mgr._parse_size_to_bytes("100 KB") == 100_000
    assert mgr._parse_size_to_bytes("N/A") == 0
    assert mgr._parse_size_to_bytes("") == 0

def test_find_user_data(test_env):
    mgr = UninstallManager()
    
    # Create some dummy config folders
    config_dir = test_env / ".config/myapp"
    config_dir.mkdir(parents=True)
    cache_dir = test_env / ".cache/myapp"
    cache_dir.mkdir(parents=True)
    flatpak_data = test_env / ".var/app/com.example.MyApp"
    flatpak_data.mkdir(parents=True)
    
    paths = mgr._find_user_data("myapp")
    assert config_dir in paths
    assert cache_dir in paths
    
    # Check variant matching
    paths_variant = mgr._find_user_data("My-App")
    assert config_dir in paths_variant # variant 'myapp' should match 'myapp' folder

@patch("shutil.which")
@patch("subprocess.run")
def test_scan_flatpaks(mock_run, mock_which):
    mock_which.return_value = "/usr/bin/flatpak"
    mock_run.return_value = MagicMock(
        returncode=0, 
        stdout="MyApp\tcom.example.MyApp\t1.2 GB\n"
    )
    
    mgr = UninstallManager()
    mgr.scan_flatpaks()
    
    assert len(mgr.apps) == 1
    assert mgr.apps[0]['name'] == "com.example.MyApp"
    assert mgr.apps[0]['type'] == "Flatpak"
    assert mgr.apps[0]['size_bytes'] == 1200_000_000

@patch("src.manage.uninstall.run_command")
def test_uninstall_app_flatpak(mock_run_cmd, test_env):
    mgr = UninstallManager()
    mgr.apps = [{
        "name": "MyApp",
        "id": "com.example.MyApp",
        "type": "Flatpak",
        "size_bytes": 1000,
        "data_paths": []
    }]
    
    mock_run_cmd.return_value = MagicMock(returncode=0)
    
    success, freed, details = mgr.uninstall_app(0)
    
    assert success is True
    assert freed == 1000
    mock_run_cmd.assert_called_with(["flatpak", "uninstall", "-y", "com.example.MyApp"], capture=False)
