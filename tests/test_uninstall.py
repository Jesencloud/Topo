from unittest.mock import MagicMock, patch

from src.clean.app_manager import UninstallManager, run_uninstall
from src.core.history import parse_deletion_history


def test_run_uninstall_no_apps():
    with (
        patch("src.clean.app_manager.UninstallManager.run_full_scan", return_value=[]),
        patch("src.ui.navigator.Navigator.wait_for_return") as mock_wait,
    ):
        run_uninstall()
        mock_wait.assert_called_once()


def test_run_uninstall_escape_selector():
    mock_apps = [
        {"id": "test", "name": "Test", "size_bytes": 100, "size_str": "100B", "type": "DNF"}
    ]
    with (
        patch("src.clean.app_manager.UninstallManager.run_full_scan", return_value=mock_apps),
        patch("src.clean.app_manager.UninstallSelector.run", return_value=[]),
    ):
        run_uninstall()


@patch("sys.stdin.fileno", return_value=0)
@patch("sys.stdin.read", return_value="\n")
@patch("termios.tcgetattr", return_value=[])
@patch("termios.tcsetattr")
@patch("tty.setcbreak")
@patch("src.ui.navigator.Navigator.raw_mode")
def test_run_uninstall_execute_and_exit(
    mock_raw, mock_setcbreak, mock_setattr, mock_getattr, mock_read, mock_fileno
):
    mock_apps = [
        {
            "id": "test",
            "name": "Test",
            "size_bytes": 100,
            "size_str": "100B",
            "type": "DNF",
            "install_time": 0,
        }
    ]
    # Mock raw_mode to act as a proper context manager
    mock_raw.return_value.__enter__.return_value = 0

    with (
        patch("src.clean.app_manager.UninstallManager.run_full_scan", return_value=mock_apps),
        patch("src.clean.app_manager.UninstallSelector.run", return_value=[0]),
        patch("src.clean.app_manager.UninstallManager.execute_uninstall") as mock_exec,
        patch("src.ui.navigator.Navigator.wait_for_return", return_value=False),
        patch("src.ui.navigator.Navigator.get_key", return_value="\n"),
        patch("src.core.system.ensure_sudo_session", return_value=True),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=1)
        run_uninstall()
        mock_exec.assert_called_once()


@patch("sys.stdin.fileno", return_value=0)
@patch("sys.stdin.read", return_value="x")
@patch("termios.tcgetattr", return_value=[])
@patch("termios.tcsetattr")
@patch("tty.setcbreak")
@patch("src.ui.navigator.Navigator.raw_mode")
def test_run_uninstall_cancel(
    mock_raw, mock_setcbreak, mock_setattr, mock_getattr, mock_read, mock_fileno
):
    mock_apps = [
        {
            "id": "test",
            "name": "Test",
            "size_bytes": 100,
            "size_str": "100B",
            "type": "DNF",
            "install_time": 0,
        }
    ]
    mock_raw.return_value.__enter__.return_value = 0

    # We need side_effect to stop the while True loop after one iteration
    def mock_scan_side_effect():
        if not hasattr(mock_scan_side_effect, "called"):
            mock_scan_side_effect.called = True
            return mock_apps
        return []

    with (
        patch(
            "src.clean.app_manager.UninstallManager.run_full_scan",
            side_effect=mock_scan_side_effect,
        ),
        patch("src.clean.app_manager.UninstallSelector.run", return_value=[0]),
        patch("src.clean.app_manager.UninstallManager.execute_uninstall") as mock_exec,
        patch("src.ui.navigator.Navigator.wait_for_return", return_value=False),
        patch("src.ui.navigator.Navigator.get_key", return_value="\x1b"),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=1)
        run_uninstall()
        assert not mock_exec.called


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


def test_find_residue_paths_ignores_generic_short_tail_tokens(test_env):
    mgr = UninstallManager()

    (test_env / ".cache/go").mkdir(parents=True)
    (test_env / ".config/code").mkdir(parents=True)
    (test_env / ".local/share/id").mkdir(parents=True)

    with patch("pathlib.Path.home", return_value=test_env):
        assert mgr.find_residue_paths("org.example.go", "Example Go", "Flatpak") == []
        assert mgr.find_residue_paths("org.example.code", "Example Code", "Flatpak") == []
        assert mgr.find_residue_paths("org.example.id", "Example Id", "Flatpak") == []


def test_find_residue_paths_allows_specific_prefix_and_substring(test_env):
    mgr = UninstallManager()

    telegram_cache = test_env / ".cache/telegram-desktop"
    myapp_state = test_env / ".local/share/vendor-myapp-state"
    telegram_cache.mkdir(parents=True)
    myapp_state.mkdir(parents=True)

    with patch("pathlib.Path.home", return_value=test_env):
        telegram_paths = mgr.find_residue_paths("org.telegram.desktop", "Telegram", "Flatpak")
        myapp_paths = mgr.find_residue_paths("com.example.myapp", "MyApp", "Flatpak")

    assert telegram_cache in telegram_paths
    assert myapp_state in myapp_paths


def test_find_residue_paths_skips_official_only_apps(test_env):
    mgr = UninstallManager()

    vpn_config = test_env / ".config/tailscale"
    input_config = test_env / ".config/fcitx5"
    vpn_config.mkdir(parents=True)
    input_config.mkdir(parents=True)

    with patch("pathlib.Path.home", return_value=test_env):
        assert mgr.find_residue_paths("tailscale", "Tailscale VPN", "DNF") == []
        assert mgr.find_residue_paths("org.fcitx.Fcitx5", "Fcitx5", "Flatpak") == []


@patch("shutil.which")
@patch("subprocess.run")
def test_run_full_scan_rpm(mock_run, mock_which):
    mock_which.side_effect = lambda x: "/usr/bin/rpm" if x == "rpm" else None
    # Name\tSize\tInstallTime
    # Make size > 100MB (104857600) to pass the new user app filter
    mock_run.return_value = MagicMock(returncode=0, stdout="heavy-app\t150000000\t1700000000\n")

    mgr = UninstallManager()
    with patch("src.core.system.get_os_id", return_value="fedora"):
        apps = mgr.run_full_scan()

    assert len(apps) >= 1
    heavy_app = next((a for a in apps if a["id"] == "heavy-app"), None)
    assert heavy_app is not None
    assert heavy_app["size_bytes"] == 150000000
    assert heavy_app["install_time"] == 1700000000


@patch("shutil.which")
@patch("subprocess.run")
def test_run_full_scan_skips_system_components(mock_run, mock_which):
    mock_which.side_effect = lambda x: "/usr/bin/rpm" if x == "rpm" else None
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="nvidia-driver\t200000000\t1700000000\nkernel-core\t200000000\t1700000000\n",
    )

    mgr = UninstallManager()
    with patch("src.core.system.get_os_id", return_value="fedora"):
        apps = mgr.run_full_scan()

    assert apps == []


@patch("shutil.which")
@patch("subprocess.run")
def test_run_full_scan_flatpaks(mock_run, mock_which):
    mock_which.side_effect = lambda x: "/usr/bin/flatpak" if x == "flatpak" else None
    mock_run.return_value = MagicMock(returncode=0, stdout="MyApp\tcom.example.MyApp\t1.2 GB\n")

    mgr = UninstallManager()
    with patch("src.core.system.get_os_id", return_value="fedora"):
        apps = mgr.run_full_scan()

    assert len(apps) >= 1
    # Find our app in the results
    myapp = next((a for a in apps if a["id"] == "com.example.MyApp"), None)
    assert myapp is not None
    assert myapp["name"] == "MyApp"
    assert myapp["type"] == "Flatpak"


@patch("src.clean.app_manager.system.run_command")
@patch("shutil.which")
def test_run_full_scan_snaps(mock_which, mock_run_cmd):
    mock_which.side_effect = lambda x: "/usr/bin/snap" if x == "snap" else None
    mock_run_cmd.return_value = MagicMock(
        ok=True,
        stdout="Name Version Rev Tracking Publisher Notes\nmy-snap 1.0 1 latest/stable test -\n",
    )

    apps = UninstallManager().run_full_scan()

    assert apps == [
        {
            "id": "my-snap",
            "name": "my-snap",
            "size_bytes": 0,
            "size_str": "N/A",
            "type": "Snap",
            "install_time": 0,
        }
    ]


@patch("src.core.system.run_command")
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


@patch("src.core.system.run_command")
def test_execute_uninstall_snap(mock_run_cmd, test_env):
    mgr = UninstallManager()
    app = {
        "name": "MySnap",
        "id": "my-snap",
        "type": "Snap",
        "size_bytes": 0,
    }
    mock_run_cmd.return_value = MagicMock(ok=True)

    with patch("pathlib.Path.home", return_value=test_env):
        details = mgr.execute_uninstall(app, [])

    assert details == []
    mock_run_cmd.assert_any_call(["snap", "remove", "my-snap"], use_sudo=True, capture=True)


@patch("src.core.system.run_command")
@patch("subprocess.run")
def test_execute_uninstall_dnf(mock_run, mock_run_cmd, test_env):
    mgr = UninstallManager()
    app = {
        "name": "HeavyApp",
        "id": "heavy-app",
        "type": "DNF",
        "size_bytes": 150000000,
    }

    # Simulate app is running, so pgrep returns 0, then pkill is called
    mock_run.return_value = MagicMock(returncode=0)
    mock_run_cmd.return_value = MagicMock(returncode=0)

    with patch("pathlib.Path.home", return_value=test_env):
        # Pass a dummy path to ensure safe_remove logic is at least executed
        dummy_path = test_env / ".config/heavy-app"
        dummy_path.mkdir(parents=True)
        details = mgr.execute_uninstall(app, [dummy_path])

    assert isinstance(details, list)
    assert len(details) == 1
    # Check DNF removal command
    mock_run_cmd.assert_called_with(
        ["dnf", "remove", "-y", "heavy-app"], use_sudo=True, capture=True
    )
    # Check that pkill was called since we mocked pgrep to succeed
    assert any("pkill" in str(call) for call in mock_run_cmd.call_args_list)


@patch("src.core.system.run_command")
def test_execute_uninstall_writes_history_for_package_only(mock_run_cmd, test_env, monkeypatch):
    log_path = test_env / "state" / "topo" / "deletions.log"
    monkeypatch.setenv("TOPO_DELETE_LOG", str(log_path))
    mock_run_cmd.return_value = MagicMock(ok=True)
    mgr = UninstallManager()
    app = {
        "name": "NoResidue",
        "id": "no-residue",
        "type": "DNF",
        "size_bytes": 2048,
    }

    details = mgr.execute_uninstall(app, [])

    assert details == []
    sessions = parse_deletion_history(log_path)
    assert len(sessions) == 1
    assert sessions[0].command == "uninstall NoResidue"
    assert sessions[0].removed == 1
    assert sessions[0].total_size == 2048


def test_get_app_localized_name(test_env):
    mgr = UninstallManager()
    desktop_file = test_env / "test.desktop"

    # Test Name[zh_CN] priority
    desktop_file.write_text("Name=EnglishName\nName[zh_CN]=中文名字\n")
    name = mgr._get_app_localized_name(desktop_file, "fallback")
    assert name == "中文名字"

    # Test Name fallback
    desktop_file.write_text("Exec=test\nName=EnglishName\n")
    name = mgr._get_app_localized_name(desktop_file, "fallback")
    assert name == "EnglishName"

    # Test complete fallback
    desktop_file.write_text("Exec=test\n")
    name = mgr._get_app_localized_name(desktop_file, "fallback")
    assert name == "fallback"


def test_get_app_keywords(test_env):
    mgr = UninstallManager()
    desktop_file = test_env / "test.desktop"

    desktop_file.write_text("Exec=/usr/bin/my-app --arg\nIcon=my-app-icon\n")
    keywords = mgr._get_app_keywords(desktop_file)

    assert "my-app" in keywords
    assert "my-app-icon" in keywords
