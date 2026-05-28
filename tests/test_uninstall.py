from unittest.mock import MagicMock, patch

from src.clean.app_manager import UninstallManager


def test_parse_size_to_bytes():
    mgr = UninstallManager()
    # Now using Base-2 (1024)
    assert mgr._parse_size_to_bytes("1 GB") == 1024**3
    assert mgr._parse_size_to_bytes("500 MB") == 500 * 1024**2
    assert mgr._parse_size_to_bytes("100 KB") == 100 * 1024
    assert mgr._parse_size_to_bytes("N/A") == 0
    assert mgr._parse_size_to_bytes("") == 0


def test_find_residue_paths(test_env):
    mgr = UninstallManager()

    # Create some dummy config folders
    config_dir = test_env / ".config/myapp"
    config_dir.mkdir(parents=True)
    cache_dir = test_env / ".cache/myapp"
    cache_dir.mkdir(parents=True)

    with patch("pathlib.Path.home", return_value=test_env):
        paths = mgr.find_residue_paths("myapp", "MyApp", "DNF")
        assert any("myapp" in str(p).lower() for p in paths)


@patch("shutil.which")
@patch("subprocess.run")
def test_run_full_scan_flatpaks(mock_run, mock_which):
    mock_which.side_effect = lambda x: "/usr/bin/flatpak" if x == "flatpak" else None
    mock_run.return_value = MagicMock(returncode=0, stdout="MyApp\tcom.example.MyApp\t1.2 GB\n")

    mgr = UninstallManager()
    with patch("src.clean.app_manager.get_os_id", return_value="fedora"):
        apps = mgr.run_full_scan()

    assert len(apps) >= 1
    # Find our app in the results
    myapp = next((a for a in apps if a["id"] == "com.example.MyApp"), None)
    assert myapp is not None
    assert myapp["name"] == "MyApp"
    assert myapp["type"] == "Flatpak"


@patch("src.clean.app_manager.run_command")
@patch("subprocess.run")
def test_execute_uninstall_flatpak(mock_run, mock_run_cmd, test_env):
    mgr = UninstallManager()
    app = {
        "name": "MyApp",
        "id": "com.example.MyApp",
        "type": "Flatpak",
        "size_bytes": 1000,
    }

    mock_run.return_value = MagicMock(returncode=1)  # No process running
    mock_run_cmd.return_value = MagicMock(returncode=0)

    with patch("pathlib.Path.home", return_value=test_env):
        details = mgr.execute_uninstall(app, [])

    assert isinstance(details, list)
    mock_run_cmd.assert_called_with(
        ["flatpak", "uninstall", "-y", "com.example.MyApp"], capture=True
    )
