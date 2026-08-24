import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.history import parse_deletion_history
from src.ui.screens.uninstall import run_uninstall
from src.uninstall import UninstallManager, _ResidueEntryIndex


@pytest.fixture(autouse=True)
def mock_sleep():
    """Mock time.sleep globally for all uninstall tests to prevent slow test execution."""
    UninstallManager.clear_scan_cache()
    with patch("time.sleep") as m:
        yield m
    UninstallManager.clear_scan_cache()


@pytest.fixture
def deterministic_gram_buckets(monkeypatch):
    """Pin gram bucketing to a stable hash so selectivity assertions are seed-proof.

    Production buckets 5-grams with the per-process-randomized ``str.__hash__``
    (see ``_ResidueEntryIndex._gram_bucket``). That is correct there, but any test
    that asserts *how much* the index narrows is otherwise flaky: on roughly one
    ``PYTHONHASHSEED`` in a few hundred, a gram shared by the noise entries hashes
    into the query's bucket and collapses the narrowing to the O(N) worst case.
    Swapping in a stable ``crc32`` for the test keeps the narrowing logic under
    test while making the outcome reproducible; which entries actually match never
    depended on the hash, so correctness coverage is unaffected.
    """
    monkeypatch.setattr(
        _ResidueEntryIndex,
        "_gram_bucket",
        classmethod(lambda cls, gram: zlib.crc32(gram.encode()) % cls._GRAM_BUCKET_COUNT),
    )


def test_run_uninstall_no_apps():
    with (
        patch("src.uninstall.UninstallManager.run_full_scan", return_value=[]),
        patch("src.ui.navigator.Navigator.wait_for_return") as mock_wait,
    ):
        run_uninstall()
        mock_wait.assert_called_once()


def test_run_uninstall_escape_selector():
    mock_apps = [
        {"id": "test", "name": "Test", "size_bytes": 100, "size_str": "100B", "type": "DNF"}
    ]
    with (
        patch("src.uninstall.UninstallManager.run_full_scan", return_value=mock_apps),
        patch("src.ui.screens.uninstall.UninstallSelector.run", return_value=[]),
    ):
        run_uninstall()


def test_run_uninstall_execute_and_exit():
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
    with (
        patch("src.uninstall.UninstallManager.run_full_scan", return_value=mock_apps),
        patch("src.ui.screens.uninstall.UninstallSelector.run", return_value=[0]),
        patch("src.ui.screens.uninstall.UninstallPreviewSelector.run", return_value=True),
        patch("src.uninstall.UninstallManager.execute_uninstall") as mock_exec,
        patch("src.ui.navigator.Navigator.wait_for_return", return_value=False),
        patch("src.core.system.ensure_sudo_session", return_value=True),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=1)
        mock_exec.return_value = {"package_removed": True, "removed_paths": []}
        run_uninstall()
        mock_exec.assert_called_once()


def test_run_uninstall_cancel():
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

    # We need side_effect to stop the while True loop after one iteration
    def mock_scan_side_effect(*args, **kwargs):
        if not hasattr(mock_scan_side_effect, "called"):
            mock_scan_side_effect.called = True
            return mock_apps
        return []

    with (
        patch(
            "src.uninstall.UninstallManager.run_full_scan",
            side_effect=mock_scan_side_effect,
        ),
        patch("src.ui.screens.uninstall.UninstallSelector.run", return_value=[0]),
        patch("src.ui.screens.uninstall.UninstallPreviewSelector.run", return_value=False),
        patch("src.uninstall.UninstallManager.execute_uninstall") as mock_exec,
        patch("src.ui.navigator.Navigator.wait_for_return", return_value=False),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=1)
        run_uninstall()
        assert not mock_exec.called


def test_parse_size_to_bytes():
    from src.core.file_ops import parse_size_to_bytes

    # Now using Base-2 (1024)
    assert parse_size_to_bytes("1 GB") == 1024**3
    assert parse_size_to_bytes("500 MB") == 500 * 1024**2
    assert parse_size_to_bytes("100 KB") == 100 * 1024
    assert parse_size_to_bytes("N/A") == 0
    assert parse_size_to_bytes("") == 0


def test_find_residue_paths(test_env):
    mgr = UninstallManager()

    # Create some dummy config folders
    config_dir = test_env / ".config/myapp"
    config_dir.mkdir(parents=True)
    cache_dir = test_env / ".cache/myapp"
    cache_dir.mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        paths = mgr.find_residue_paths("myapp", "MyApp")
        assert any("myapp" in str(p).lower() for p in paths)


def test_residue_shared_indexes_are_scanned_once(test_env):
    mgr = UninstallManager()
    icon = test_env / ".local/share/icons/theme/myapp.png"
    service = test_env / ".config/systemd/user/myapp.service"
    hidden = test_env / ".myapp"
    icon.parent.mkdir(parents=True)
    service.parent.mkdir(parents=True)
    hidden.mkdir()
    icon.write_text("icon")
    service.write_text("[Service]")

    with patch("pathlib.Path.home", return_value=test_env):
        index = mgr._pre_scan_search_roots()
        with (
            patch("pathlib.Path.rglob", side_effect=AssertionError("rescanned icons")),
            patch("pathlib.Path.glob", side_effect=AssertionError("rescanned services")),
            patch("src.uninstall.os.scandir", side_effect=AssertionError("rescanned roots")),
        ):
            paths = mgr.find_residue_paths("com.example.myapp", "MyApp", pre_scanned_entries=index)

    assert icon in paths
    assert service in paths
    assert hidden in paths


def _index_matches(index, targets):
    """What find_residue_paths() keeps: candidates that survive _name_matches()."""
    return [
        entry
        for entry in index.candidates(targets)
        if any(UninstallManager._name_matches(entry[0], target) for target in targets)
    ]


def _pad_to_indexed(entries, filler_name, test_env):
    """Grow an entry list past _MIN_ENTRIES_TO_INDEX so the lookup tables get built.

    Small roots deliberately stay unindexed, so a test that wants to exercise the
    narrowing has to be large enough to earn an index.
    """
    needed = _ResidueEntryIndex._MIN_ENTRIES_TO_INDEX - len(entries)
    padding = [(f"{filler_name}{i}", test_env / f"{filler_name}{i}") for i in range(max(0, needed))]
    return entries + padding


def test_residue_index_candidates_preserve_name_match_semantics(test_env):
    """Narrowing must not change which entries match -- in both index regimes.

    A dropped candidate would mean an uninstall silently leaves residue behind, so
    the property is checked below the indexing threshold (where candidates() hands
    back everything) and above it (where the lookup tables narrow).
    """
    entries = [
        ("myapp", test_env / "myapp"),
        ("myapp-config", test_env / "myapp-config"),
        ("vendor-myapp-state", test_env / "vendor-myapp-state"),
        ("unrelated", test_env / "unrelated"),
        ("go", test_env / "go"),
    ]
    targets = {"myapp", "go"}
    expected = [
        entry
        for entry in entries
        if any(UninstallManager._name_matches(entry[0], target) for target in targets)
    ]

    small = _ResidueEntryIndex.build(entries)
    assert not small.is_indexed
    assert small.candidates(targets) == entries  # unnarrowed, still a valid superset
    assert _index_matches(small, targets) == expected

    large = _ResidueEntryIndex.build(_pad_to_indexed(entries, "filler-", test_env))
    assert large.is_indexed
    assert _index_matches(large, targets) == expected


def test_residue_index_reduces_full_index_matching(test_env, deterministic_gram_buckets):
    entries = [(f"unrelated-{index}", test_env / str(index)) for index in range(1000)]
    expected = ("vendor-myapp-state", test_env / "match")
    entries.append(expected)
    index = _ResidueEntryIndex.build(entries)

    candidates = index.candidates({"myapp"})
    matches = [entry for entry in candidates if UninstallManager._name_matches(entry[0], "myapp")]

    assert expected in candidates
    assert matches == [expected]
    assert len(candidates) < len(entries) // 10


def test_residue_index_narrows_when_noise_shares_the_target_prefix(test_env):
    """Pins the selectivity limit: candidates() unions its three stages.

    The candidate set is therefore only as small as the *least* selective stage.
    Noise named "myapp-decoy-N" shares both the 3-char prefix bucket and the
    leading 5-gram of "myappstudio", so neither stage narrows and every entry
    becomes a candidate. That is the documented O(N) worst case, and the thing
    that must still hold is the part that matters: _name_matches() is the final
    authority, so the decoys are rejected and the result is unchanged.
    """
    entries = [(f"myapp-decoy-{i}", test_env / f"decoy-{i}") for i in range(1000)]
    target_entry = ("prefs-myappstudio", test_env / "real")
    entries.append(target_entry)
    index = _ResidueEntryIndex.build(entries)
    assert index.is_indexed

    # Worst case acknowledged rather than asserted away: no narrowing happens here.
    assert len(index.candidates({"myappstudio"})) == len(entries)
    # ...and the semantics survive it, which is the invariant worth guarding.
    assert _index_matches(index, {"myappstudio"}) == [target_entry]

    # "myapp" legitimately matches every decoy at a word boundary, so a correct
    # index keeps all of them -- narrowing must never drop a true match.
    assert _index_matches(index, {"myapp"}) == [
        entry for entry in entries if UninstallManager._name_matches(entry[0], "myapp")
    ]


def test_residue_index_narrows_when_noise_shares_neither_stage(
    test_env, deterministic_gram_buckets
):
    """The complement of the case above: distinct noise collapses the candidate set."""
    entries = [(f"zeta-decoy-{i}", test_env / f"decoy-{i}") for i in range(1000)]
    target_entry = ("prefs-myappstudio", test_env / "real")
    entries.append(target_entry)
    index = _ResidueEntryIndex.build(entries)

    candidates = index.candidates({"myappstudio"})
    assert target_entry in candidates
    assert len(candidates) < len(entries) // 10, len(candidates)
    assert _index_matches(index, {"myappstudio"}) == [target_entry]


def test_residue_index_small_root_skips_the_gram_table(test_env):
    """Roots with a handful of entries must not allocate 2048 gram arrays."""
    tiny = _ResidueEntryIndex.build([("a.service", test_env / "a.service")])

    assert not tiny.is_indexed
    assert tiny.gram_buckets == ()
    assert tiny.exact == {} and tiny.prefixes == {}


def test_residue_index_is_compared_by_identity(test_env):
    """eq=False: a generated __eq__ would walk 2048 arrays and __hash__ would raise."""
    entries = _pad_to_indexed([], "entry-", test_env)
    index = _ResidueEntryIndex.build(entries)

    assert index == index
    assert index != _ResidueEntryIndex.build(entries)
    assert hash(index) == hash(index)  # identity hash, not a field-walking one


def test_find_residue_paths_ignores_generic_short_tail_tokens(test_env):
    mgr = UninstallManager()

    (test_env / ".cache/go").mkdir(parents=True)
    (test_env / ".config/code").mkdir(parents=True)
    (test_env / ".local/share/id").mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        assert mgr.find_residue_paths("org.example.go", "Example Go") == []
        assert mgr.find_residue_paths("org.example.code", "Example Code") == []
        assert mgr.find_residue_paths("org.example.id", "Example Id") == []


def test_find_residue_paths_allows_specific_prefix_and_substring(test_env):
    mgr = UninstallManager()

    telegram_cache = test_env / ".cache/telegram-desktop"
    myapp_state = test_env / ".local/share/vendor-myapp-state"
    telegram_cache.mkdir(parents=True)
    myapp_state.mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        telegram_paths = mgr.find_residue_paths("org.telegram.desktop", "Telegram")
        myapp_paths = mgr.find_residue_paths("com.example.myapp", "MyApp")

    assert telegram_cache in telegram_paths
    assert myapp_state in myapp_paths


def test_find_residue_paths_skips_official_only_apps(test_env):
    mgr = UninstallManager()

    vpn_config = test_env / ".config/tailscale"
    input_config = test_env / ".config/fcitx5"
    vpn_config.mkdir(parents=True)
    input_config.mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        assert mgr.find_residue_paths("tailscale", "Tailscale VPN") == []
        assert mgr.find_residue_paths("org.fcitx.Fcitx5", "Fcitx5") == []


@patch("shutil.which")
@patch("subprocess.run")
def test_run_full_scan_rpm(mock_run, mock_which):
    mock_which.side_effect = lambda x: "/usr/bin/rpm" if x == "rpm" else None
    # Name\tSize\tInstallTime
    # Make size > 100MB (104857600) to pass the new user app filter
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=("older-heavy-app\t250000000\t1600000000\nheavy-app\t150000000\t1700000000\n"),
    )

    mgr = UninstallManager()
    with (
        patch("src.core.system.get_os_id", return_value="fedora"),
        patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")),
    ):
        apps = mgr.run_full_scan()

    assert len(apps) >= 1
    heavy_app = next((a for a in apps if a["id"] == "heavy-app"), None)
    assert heavy_app is not None
    assert heavy_app["size_bytes"] == 150000000
    assert heavy_app["install_time"] == 1700000000
    assert apps[0]["id"] == "heavy-app"


@patch("shutil.which")
@patch("subprocess.run")
def test_run_full_scan_reuses_short_lived_cache(mock_run, mock_which):
    mock_which.side_effect = lambda x: "/usr/bin/rpm" if x == "rpm" else None
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="heavy-app\t150000000\t1700000000\n",
    )

    with patch("src.core.system.get_os_id", return_value="fedora"):
        first = UninstallManager().run_full_scan(use_cache=True)
        mock_run.reset_mock()
        second = UninstallManager().run_full_scan(use_cache=True)

    assert first == second
    mock_run.assert_not_called()


@patch("shutil.which")
@patch("subprocess.run")
def test_run_full_scan_skips_system_components(mock_run, mock_which):
    mock_which.side_effect = lambda x: "/usr/bin/rpm" if x == "rpm" else None
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=(
            "nvidia-driver\t200000000\t1700000000\n"
            "gcc\t200000000\t1700000000\n"
            "gcc-c++\t200000000\t1700000000\n"
            "kernel-core\t200000000\t1700000000\n"
            "gdm\t200000000\t1700000000\n"
            "gnome-browser-connector\t200000000\t1700000000\n"
            "gnome-color-manager\t200000000\t1700000000\n"
            "gnome-control-center\t200000000\t1700000000\n"
            "gnome-disk-utility\t200000000\t1700000000\n"
            "gnome-initial-setup\t200000000\t1700000000\n"
            "gnome-logs\t200000000\t1700000000\n"
            "gnome-online-accounts\t200000000\t1700000000\n"
            "gnome-settings-daemon\t200000000\t1700000000\n"
            "gnome-software\t200000000\t1700000000\n"
            "gnome-system-monitor\t200000000\t1700000000\n"
            "gnome-terminal\t200000000\t1700000000\n"
            "ptyxis\t200000000\t1700000000\n"
            "nautilus\t200000000\t1700000000\n"
            "gvfs\t200000000\t1700000000\n"
            "dconf\t200000000\t1700000000\n"
            "ibus-libpinyin\t200000000\t1700000000\n"
            "ibus-hangul\t200000000\t1700000000\n"
            "ibus-chewing\t200000000\t1700000000\n"
            "ibus-anthy\t200000000\t1700000000\n"
            "libreoffice-core\t200000000\t1700000000\n"
            "libreoffice-xsltfilter\t200000000\t1700000000\n"
            "xdg-desktop-portal\t200000000\t1700000000\n"
            "xdg-desktop-portal-gnome\t200000000\t1700000000\n"
        ),
    )

    mgr = UninstallManager()
    with (
        patch("src.core.system.get_os_id", return_value="fedora"),
        patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")),
    ):
        apps = mgr.run_full_scan()

    assert apps == []


@patch("shutil.which")
@patch("subprocess.run")
def test_run_full_scan_filters_gnome_default_apps(mock_run, mock_which):
    mock_which.side_effect = lambda x: "/usr/bin/rpm" if x == "rpm" else None
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=(
            "gnome-calculator\t200000000\t1700000000\n"
            "gnome-calendar\t200000000\t1700000000\n"
            "gnome-characters\t200000000\t1700000000\n"
            "gnome-clocks\t200000000\t1700000000\n"
            "gnome-connections\t200000000\t1700000000\n"
            "gnome-contacts\t200000000\t1700000000\n"
            "gnome-font-viewer\t200000000\t1700000000\n"
            "gnome-maps\t200000000\t1700000000\n"
        ),
    )

    mgr = UninstallManager()
    with (
        patch("src.core.system.get_os_id", return_value="fedora"),
        patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")),
    ):
        apps = mgr.run_full_scan()

    assert apps == []


@patch("shutil.which")
@patch("subprocess.run")
def test_run_full_scan_keeps_user_libreoffice_apps(mock_run, mock_which):
    mock_which.side_effect = lambda x: "/usr/bin/rpm" if x == "rpm" else None
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout=(
            "libreoffice-writer\t200000000\t1700000000\n"
            "libreoffice-calc\t200000000\t1700000000\n"
            "libreoffice-impress\t200000000\t1700000000\n"
        ),
    )

    mgr = UninstallManager()
    with (
        patch("src.core.system.get_os_id", return_value="fedora"),
        patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")),
    ):
        apps = mgr.run_full_scan()

    assert [app["id"] for app in apps] == [
        "libreoffice-writer",
        "libreoffice-calc",
        "libreoffice-impress",
    ]


@patch("src.uninstall.system.run_command")
@patch("shutil.which")
def test_run_full_scan_apt(mock_which, mock_run_cmd):
    mock_which.side_effect = lambda x: "/usr/bin/dpkg-query" if x == "dpkg-query" else None
    mock_run_cmd.return_value = MagicMock(
        ok=True,
        stdout=("firefox\t204800\nlinux-image-generic\t204800\nlibreoffice-writer:amd64\t204800\n"),
    )

    mgr = UninstallManager()
    with (
        patch("src.core.system.get_os_id", return_value="ubuntu"),
        patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")),
    ):
        apps = mgr.run_full_scan()

    assert [(app["id"], app["type"]) for app in apps] == [
        ("firefox", "APT"),
        ("libreoffice-writer", "APT"),
    ]


@patch("src.uninstall.system.run_command")
@patch("shutil.which")
def test_run_full_scan_pacman(mock_which, mock_run_cmd):
    mock_which.side_effect = lambda x: "/usr/bin/pacman" if x == "pacman" else None
    mock_run_cmd.return_value = MagicMock(
        ok=True,
        stdout=(
            "Name            : firefox\n"
            "Installed Size  : 220.00 MiB\n"
            "\n"
            "Name            : systemd\n"
            "Installed Size  : 120.00 MiB\n"
            "\n"
            "Name            : vlc\n"
            "Installed Size  : 150.00 MiB\n"
        ),
    )

    mgr = UninstallManager()
    with (
        patch("src.core.system.get_os_id", return_value="arch"),
        patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")),
    ):
        apps = mgr.run_full_scan()

    assert [(app["id"], app["type"]) for app in apps] == [
        ("firefox", "Pacman"),
        ("vlc", "Pacman"),
    ]


@patch("src.uninstall.system.run_command")
@patch("shutil.which")
def test_every_scanned_command_asks_for_the_c_locale(mock_which, mock_run_cmd):
    """Each of these replies gets parsed, and every one of them is translatable.

    rpm prints "file X is not owned by any package" and pacman "X is owned by Y"
    through gettext, so a zh_CN or de_DE desktop hands back text the parsers below
    do not recognise -- or worse, text they mistake for a package name.
    """
    mock_which.side_effect = lambda x: f"/usr/bin/{x}"
    mock_run_cmd.return_value = MagicMock(ok=True, stdout="")

    with (
        patch("src.core.system.get_os_id", return_value="fedora"),
        patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")),
    ):
        UninstallManager().run_full_scan()

    assert mock_run_cmd.call_args_list, "the scan ran no commands, so it proves nothing"
    for call in mock_run_cmd.call_args_list:
        env = call.kwargs.get("env") or {}
        assert env.get("LC_ALL") == "C", call.args[0]
        assert env.get("LANG") == "C", call.args[0]


@patch("shutil.which")
def test_pre_scan_drops_a_batch_whose_rpm_reply_is_a_line_short(mock_which, tmp_path):
    """rpm answers positionally, so a missing line shifts every later name.

    An unreadable path -- /usr/share/applications/firefox.desktop is a dangling
    symlink on some Fedora installs -- is reported on stderr, so stdout comes back
    one line short and each remaining name lands on the wrong package.
    """
    mock_which.side_effect = lambda x: "/usr/bin/rpm" if x == "rpm" else None
    apps_dir = tmp_path / ".local/share/applications"
    apps_dir.mkdir(parents=True)
    (apps_dir / "kept.desktop").write_text("[Desktop Entry]\nName=Kept\n")
    (apps_dir / "other.desktop").write_text("[Desktop Entry]\nName=Other\n")

    def one_answer_short(args, **kwargs):
        queried = [a for a in args if a.endswith(".desktop")]
        return MagicMock(ok=True, stdout="".join(f"pkg{i}\n" for i in range(len(queried) - 1)))

    with (
        patch("src.uninstall.system.run_command", side_effect=one_answer_short),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        packages, names = UninstallManager()._pre_scan_package_desktop_names()

    assert packages == set()
    assert names == {}


@patch("shutil.which")
def test_pre_scan_maps_display_names_when_the_rpm_reply_lines_up(mock_which, tmp_path):
    """The length check above must not cost us the names in the normal case."""
    mock_which.side_effect = lambda x: "/usr/bin/rpm" if x == "rpm" else None
    apps_dir = tmp_path / ".local/share/applications"
    apps_dir.mkdir(parents=True)
    (apps_dir / "kept.desktop").write_text("[Desktop Entry]\nName=Kept\n")

    def one_answer_each(args, **kwargs):
        queried = [a for a in args if a.endswith(".desktop")]
        return MagicMock(ok=True, stdout="".join(f"pkg-{Path(p).stem}\n" for p in queried))

    with (
        patch("src.uninstall.system.run_command", side_effect=one_answer_each),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        packages, names = UninstallManager()._pre_scan_package_desktop_names()

    assert "pkg-kept" in packages
    assert names["pkg-kept"] == "Kept"


@patch("shutil.which")
def test_pre_scan_never_queries_a_dangling_desktop_symlink(mock_which, tmp_path):
    """Keeping unreadable paths out of the batch is what keeps the reply aligned."""
    mock_which.side_effect = lambda x: "/usr/bin/rpm" if x == "rpm" else None
    apps_dir = tmp_path / ".local/share/applications"
    apps_dir.mkdir(parents=True)
    (apps_dir / "real.desktop").write_text("[Desktop Entry]\nName=Real\n")
    (apps_dir / "ghost.desktop").symlink_to(tmp_path / "gone.desktop")

    queried: list[str] = []

    def record(args, **kwargs):
        queried.extend(a for a in args if a.endswith(".desktop"))
        return MagicMock(ok=True, stdout="")

    with (
        patch("src.uninstall.system.run_command", side_effect=record),
        patch("pathlib.Path.home", return_value=tmp_path),
    ):
        UninstallManager()._pre_scan_package_desktop_names()

    assert str(apps_dir / "real.desktop") in queried
    assert str(apps_dir / "ghost.desktop") not in queried


@patch("shutil.which")
@patch("subprocess.run")
def test_run_full_scan_flatpaks(mock_run, mock_which):
    mock_which.side_effect = lambda x: "/usr/bin/flatpak" if x == "flatpak" else None
    mock_run.return_value = MagicMock(returncode=0, stdout="MyApp\tcom.example.MyApp\t1.2 GB\n")

    mgr = UninstallManager()
    with (
        patch("src.core.system.get_os_id", return_value="fedora"),
        patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")),
    ):
        apps = mgr.run_full_scan()

    assert len(apps) >= 1
    # Find our app in the results
    myapp = next((a for a in apps if a["id"] == "com.example.MyApp"), None)
    assert myapp is not None
    assert myapp["name"] == "MyApp"
    assert myapp["type"] == "Flatpak"


@patch("src.uninstall.system.run_command")
@patch("shutil.which")
def test_run_full_scan_snaps(mock_which, mock_run_cmd):
    mock_which.side_effect = lambda x: "/usr/bin/snap" if x == "snap" else None
    mock_run_cmd.return_value = MagicMock(
        ok=True,
        stdout="Name Version Rev Tracking Publisher Notes\nmy-snap 1.0 1 latest/stable test -\n",
    )

    with patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")):
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

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        details = mgr.execute_uninstall(app, [])

    assert details["removed_paths"] == []
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

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        details = mgr.execute_uninstall(app, [])

    assert details["removed_paths"] == []
    mock_run_cmd.assert_any_call(
        ["snap", "remove", "--purge", "my-snap"], use_sudo=True, capture=True
    )


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

    with (
        patch("shutil.which", side_effect=lambda x: "/usr/bin/dnf" if x == "dnf" else None),
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        # Pass a dummy path to ensure safe_remove logic is at least executed
        dummy_path = test_env / ".config/heavy-app"
        dummy_path.mkdir(parents=True)
        details = mgr.execute_uninstall(app, [dummy_path])

    assert details["package_removed"] is True
    assert len(details["removed_paths"]) == 1
    # Check DNF removal command
    mock_run_cmd.assert_called_with(
        ["dnf", "remove", "-y", "heavy-app"], use_sudo=True, capture=True
    )
    # Check that pkill was called since we mocked pgrep to succeed
    assert any("pkill" in str(call) for call in mock_run_cmd.call_args_list)


@patch("src.core.system.run_command")
def test_execute_uninstall_apt(mock_run_cmd, test_env):
    mgr = UninstallManager()
    app = {
        "name": "Firefox",
        "id": "firefox",
        "type": "APT",
        "size_bytes": 150000000,
    }
    mock_run_cmd.return_value = MagicMock(ok=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        details = mgr.execute_uninstall(app, [])

    assert details["removed_paths"] == []
    mock_run_cmd.assert_any_call(["apt", "purge", "-y", "firefox"], use_sudo=True, capture=True)


@patch("src.core.system.run_command")
def test_execute_uninstall_pacman(mock_run_cmd, test_env):
    mgr = UninstallManager()
    app = {
        "name": "Firefox",
        "id": "firefox",
        "type": "Pacman",
        "size_bytes": 150000000,
    }
    mock_run_cmd.return_value = MagicMock(ok=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        details = mgr.execute_uninstall(app, [])

    assert details["removed_paths"] == []
    mock_run_cmd.assert_any_call(
        ["pacman", "-Rns", "--noconfirm", "firefox"], use_sudo=True, capture=True
    )


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

    assert details["removed_paths"] == []
    sessions = parse_deletion_history(log_path)
    assert len(sessions) == 1
    assert sessions[0].command == "uninstall NoResidue"
    assert sessions[0].removed == 1
    assert sessions[0].total_size == 2048


def test_get_app_keywords(test_env):
    mgr = UninstallManager()
    desktop_file = test_env / "test.desktop"

    desktop_file.write_text("Exec=/usr/bin/my-app --arg\nIcon=my-app-icon\n")
    keywords = mgr._get_app_keywords(desktop_file)

    assert "my-app" in keywords
    assert "my-app-icon" in keywords


def test_find_residue_paths_never_targets_xdg_user_dirs(test_env):
    """An app whose display name is a common word (e.g. GNOME 'Music') must not
    flag standard XDG user-data directories such as ~/Music or ~/Videos."""
    mgr = UninstallManager()
    music_dir = test_env / "Music"
    videos_dir = test_env / "Videos"
    documents_dir = test_env / "Documents"
    for d in (music_dir, videos_dir, documents_dir):
        d.mkdir()
    (music_dir / "song.mp3").write_text("precious")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        music_paths = mgr.find_residue_paths("org.gnome.Music", "Music")
        videos_paths = mgr.find_residue_paths("org.gnome.Totem", "Videos")
        docs_paths = mgr.find_residue_paths("com.example.Documents", "Documents")

    assert music_dir not in music_paths
    assert videos_dir not in videos_paths
    assert documents_dir not in docs_paths


def test_uninstall_cannot_delete_xdg_user_data_dir(test_env):
    """Defense in depth: even if a user-data dir reaches safe_remove with
    allow_app_data_removal=True, it must be refused while files inside stay
    deletable."""
    from src.core.file_ops import safe_remove

    music = test_env / "Music"
    music.mkdir()
    song = music / "song.mp3"
    song.write_text("precious")

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        ok_dir, reason = safe_remove(music, use_trash=False, allow_app_data_removal=True)
        ok_file, _ = safe_remove(song, use_trash=False, allow_app_data_removal=True)

    assert ok_dir is False
    assert "user data" in reason.lower()
    assert music.exists()
    assert ok_file is True
    assert not song.exists()


def test_candidate_process_names_uses_ids_not_display_name():
    mgr = UninstallManager()
    app = {"id": "org.telegram.desktop", "name": "Telegram Desktop", "type": "Flatpak"}
    names = mgr._candidate_process_names(app)
    assert "org.telegram.desktop" in names
    assert "desktop" in names  # flatpak last segment
    assert "telegram.desktop" in names  # token split from org.
    # A localized display name with a space can never match `pkill -x`.
    assert "telegram desktop" not in names


def test_executable_names_from_desktop(test_env):
    mgr = UninstallManager()
    app_dir = test_env / ".local/share/applications"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "com.example.App.desktop").write_text(
        "[Desktop Entry]\nName=Fancy App\nExec=/usr/bin/fancy-bin %U\n"
    )
    with patch("pathlib.Path.home", return_value=test_env):
        app = {"id": "com.example.App", "name": "Fancy App", "type": "Flatpak"}
        names = mgr._candidate_process_names(app)
    assert "fancy-bin" in names


def test_candidate_process_names_ignores_unrelated_desktop_entries(test_env):
    """A short id must not drag in every entry that happens to contain those letters.

    Every name returned here is handed to `pkill -9`, and ids as short as "go",
    "qq" or "ai" appear inside plenty of unrelated .desktop file names -- matching
    those would kill programs the user never asked to remove.
    """
    mgr = UninstallManager()
    app_dir = test_env / ".local/share/applications"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "go.desktop").write_text("[Desktop Entry]\nName=Go\nExec=/usr/bin/go-gui\n")
    (app_dir / "gomuks.desktop").write_text("[Desktop Entry]\nName=Gomuks\nExec=gomuks\n")
    (app_dir / "django-admin.desktop").write_text("[Desktop Entry]\nName=Dj\nExec=django-admin\n")

    with patch("pathlib.Path.home", return_value=test_env):
        names = mgr._candidate_process_names({"id": "go", "name": "Go", "type": "DNF"})

    assert "go-gui" in names  # go.desktop is the app's own entry
    assert "gomuks" not in names
    assert "django-admin" not in names


def test_candidate_process_names_still_matches_boundary_and_reverse_dns(test_env):
    """Tightening the match must not cost the entries that really do belong."""
    mgr = UninstallManager()
    app_dir = test_env / ".local/share/applications"
    app_dir.mkdir(parents=True, exist_ok=True)
    (app_dir / "telegram-desktop.desktop").write_text(
        "[Desktop Entry]\nName=Telegram\nExec=telegram-desktop -- %u\n"
    )
    (app_dir / "org.telegram.Telegram.desktop").write_text(
        "[Desktop Entry]\nName=Telegram\nExec=/usr/bin/telegram-flatpak\n"
    )

    with patch("pathlib.Path.home", return_value=test_env):
        names = mgr._candidate_process_names({"id": "telegram", "name": "Telegram", "type": "DNF"})

    assert "telegram-desktop" in names  # word-boundary prefix
    assert "telegram-flatpak" in names  # reverse-DNS last segment


def test_run_uninstall_failed_package_not_counted(capsys):
    """A failed package removal must not be reported as freed; it goes to Failed."""
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
    with (
        patch("src.uninstall.UninstallManager.run_full_scan", return_value=mock_apps),
        patch("src.ui.screens.uninstall.UninstallSelector.run", return_value=[0]),
        patch("src.ui.screens.uninstall.UninstallPreviewSelector.run", return_value=True),
        patch("src.uninstall.UninstallManager.find_residue_paths", return_value=[]),
        patch(
            "src.uninstall.UninstallManager.execute_uninstall",
            return_value={"package_removed": False, "removed_paths": []},
        ),
        patch("src.core.system.ensure_sudo_session", return_value=True),
        patch("src.ui.navigator.Navigator.wait_for_return", return_value=False),
        patch("subprocess.run") as mock_sub,
    ):
        mock_sub.return_value = MagicMock(returncode=1)
        run_uninstall()

    out = capsys.readouterr().out
    assert "Removed 0 app(s)" in out
    assert "Failed:" in out


def test_find_residue_paths_skips_visible_home_workspace(test_env):
    """Regression (H1): a visible top-level home folder that fuzzily matches an
    app name (e.g. ~/notes-backup vs app 'Notes') must NOT be flagged as
    residue. Only hidden dot-directories at the home root are eligible, so a
    user's workspace can never be permanently removed by an uninstall."""
    mgr = UninstallManager()
    workspace = test_env / "notes-backup"  # visible workspace, prefix-matches "notes"
    hidden = test_env / ".notesapp"  # hidden dotdir residue, still eligible
    workspace.mkdir()
    hidden.mkdir()

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        paths = mgr.find_residue_paths("com.example.notes", "Notes")

    assert workspace not in paths
    assert hidden in paths


def test_execute_uninstall_residue_goes_to_trash(test_env):
    """Regression (H1): residue removal must be recoverable (use_trash=True),
    never a permanent wipe, since residue discovery is heuristic."""
    mgr = UninstallManager()
    app = {"name": "MyApp", "id": "com.example.myapp", "type": "Flatpak", "size_bytes": 0}
    residue = test_env / ".config/myapp"
    residue.mkdir(parents=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.core.system.run_command", return_value=MagicMock(ok=True, returncode=0)),
        patch("subprocess.run", return_value=MagicMock(returncode=1)),
        patch(
            "src.uninstall.safe_remove", return_value=(True, "Moved to trash")
        ) as mock_safe_remove,
    ):
        mgr.execute_uninstall(app, [residue])

    assert mock_safe_remove.call_args_list, "safe_remove was not called for residue"
    for call in mock_safe_remove.call_args_list:
        assert call.kwargs.get("use_trash") is True


def test_uninstall_helpers_and_cache_state(test_env, monkeypatch):
    mgr = UninstallManager()
    assert mgr._name_matches("firefox-profile", "firefox")
    assert not mgr._name_matches("app-data", "app")
    assert mgr._requires_official_only_uninstall("org.example.vpn", "VPN")
    assert mgr._is_system_component("libfoo", "libfoo")
    assert mgr._strip_package_arch("foo:amd64") == "foo"
    record = mgr._app_record("id", "Name", 10, "10 B", "CLI")
    assert record["type"] == "CLI"
    assert mgr.has_fresh_scan_cache() is False
    mgr.__class__._scan_cache_apps = [record]
    mgr.__class__._scan_cache_key = mgr._current_scan_cache_key()
    import time

    mgr.__class__._scan_cache_time = time.monotonic()
    assert mgr.has_fresh_scan_cache() is True
    mgr.clear_scan_cache()
    assert mgr.has_fresh_scan_cache() is False


def test_scan_package_managers_handles_missing_tools_and_bad_output():
    mgr = UninstallManager()
    with patch("src.uninstall.shutil.which", return_value=None):
        assert mgr._scan_rpm_packages(set(), {}) == []
        assert mgr._scan_apt_packages(set(), {}) == []
        assert mgr._scan_pacman_packages(set(), {}) == []
        assert mgr._scan_flatpak_apps() == []
        assert mgr._scan_snap_apps({}) == []
        assert mgr._scan_npm_global_packages() == []
    with (
        patch("src.uninstall.shutil.which", return_value="/usr/bin/rpm"),
        patch("src.uninstall.system.run_command", return_value=MagicMock(ok=True, stdout="bad\n")),
    ):
        assert mgr._scan_rpm_packages(set(), {}) == []


def test_run_full_scan_cache_and_failed_worker(monkeypatch):
    mgr = UninstallManager()
    monkeypatch.setattr(mgr, "_current_scan_cache_key", lambda: ("key",))
    monkeypatch.setattr(mgr, "_pre_scan_package_desktop_names", lambda: (set(), {}))
    monkeypatch.setattr(mgr, "_pre_scan_search_roots", lambda: {})
    monkeypatch.setattr(mgr, "_calculate_app_sizes_and_residues", lambda apps, roots: None)
    monkeypatch.setattr(
        mgr, "_scan_rpm_packages", lambda *_: [{"id": "x", "install_time": 1, "size_bytes": 1}]
    )
    monkeypatch.setattr(mgr, "_scan_apt_packages", lambda *_: [])
    monkeypatch.setattr(mgr, "_scan_pacman_packages", lambda *_: [])
    monkeypatch.setattr(mgr, "_scan_flatpak_apps", lambda: [])
    monkeypatch.setattr(mgr, "_scan_snap_apps", lambda *_: [])
    monkeypatch.setattr(mgr, "_scan_npm_global_packages", lambda: [])
    monkeypatch.setattr(mgr, "_scan_standalone_cli_apps", lambda: [])
    assert len(mgr.run_full_scan(use_cache=True)) == 1
    assert len(mgr.run_full_scan(use_cache=True)) == 1


def test_build_targets_and_execute_cli_npm_and_systemd(test_env):
    mgr = UninstallManager()
    app = {"id": "@scope/tool", "name": "tool", "type": "NPM", "size_bytes": 10}
    with (
        patch.object(mgr, "find_residue_paths", return_value=[]),
        patch.object(mgr, "_candidate_process_names", return_value=["tool"]),
        patch("src.uninstall.system.run_command", return_value=MagicMock(ok=False)),
    ):
        assert mgr.build_removal_targets([app])[0][2] is False
    with (
        patch("src.uninstall.system.run_command", return_value=MagicMock(ok=True, stdout="")),
        patch("src.uninstall.record_deletion_audit"),
        patch("src.uninstall.record_history_session"),
        patch("src.uninstall.safe_remove", return_value=(True, "ok")),
    ):
        result = mgr.execute_uninstall(app, [])
    assert result["package_removed"] is True

    service = test_env / ".config/systemd/user/app.service"
    service.parent.mkdir(parents=True)
    service.write_text("[Service]\nExecStart=/missing/app\n")
    app2 = {"id": "app", "name": "app", "type": "DNF", "size_bytes": 1}
    with (
        patch("src.uninstall.system.run_command", return_value=MagicMock(ok=True)),
        patch("src.uninstall.shutil.which", return_value="/usr/bin/systemctl"),
        patch("src.uninstall.safe_remove", return_value=(True, "ok")),
    ):
        result = mgr.execute_uninstall(app2, [service])
    assert result["removed_paths"]
