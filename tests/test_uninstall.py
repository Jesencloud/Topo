import os
import zlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.core.history import parse_deletion_history
from src.core.system import APT_NONINTERACTIVE_ENV, C_LOCALE_ENV
from src.ui.screens.uninstall import run_uninstall
from src.uninstall import (
    UninstallManager,
    _has_deb_database,
    _has_rpm_database,
    _ResidueEntryIndex,
)


@pytest.fixture(autouse=True)
def mock_sleep():
    """Mock time.sleep globally for all uninstall tests to prevent slow test execution."""
    UninstallManager.clear_scan_cache()
    with patch("time.sleep") as m:
        yield m
    UninstallManager.clear_scan_cache()


@pytest.fixture(autouse=True)
def package_databases(monkeypatch, tmp_path_factory):
    """Both package databases, populated, for every test in this module.

    The scanners refuse to fork dpkg-query or rpm when the database behind them
    holds nothing (D9). Autouse rather than opt-in because otherwise the outcome
    of every test that patches which() and feeds the scanner output depends on
    whether the *host* happens to have that database: the same tests passed on a
    Fedora workstation and failed on a GitHub runner, where /var/lib/rpm is
    absent. A test that wants the empty case monkeypatches the path itself,
    after this fixture, and says so in its name.

    tmp_path_factory and not tmp_path: several tests here point Path.home() at
    their tmp_path and scan it, and two stray database files in that tree would
    be two more entries for them to explain.
    """
    db_root = tmp_path_factory.mktemp("package-databases")
    status = db_root / "dpkg-status"
    status.write_text("Package: bash\nStatus: install ok installed\n")
    rpm_dir = db_root / "rpmdb"
    rpm_dir.mkdir()
    (rpm_dir / "rpmdb.sqlite").write_bytes(b"SQLite format 3\x00")
    monkeypatch.setattr("src.uninstall._DPKG_STATUS_FILE", status)
    monkeypatch.setattr("src.uninstall._RPM_DB_DIR", rpm_dir)
    return SimpleNamespace(deb=status, rpm=rpm_dir)


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


def _ui_app(app_type: str) -> dict[str, object]:
    return {
        "id": "test",
        "name": "Test",
        "size_bytes": 100,
        "size_str": "100B",
        "type": app_type,
        "install_time": 0,
    }


@pytest.mark.parametrize(
    ("app_type", "expect_prompt"),
    [
        ("APT", True),
        ("DNF", True),
        ("Pacman", True),
        ("Snap", True),
        ("Zypper", True),
        ("Flatpak", False),
        ("NPM", False),
        ("CLI", False),
    ],
)
def test_run_uninstall_asks_for_a_password_only_when_a_removal_needs_root(
    app_type, expect_prompt, capsys
):
    """Flatpak, NPM and CLI removals all run as the invoking user, so a password
    prompt for them is pure friction -- and a cancelled one ended the run (P5)."""
    with (
        patch("src.uninstall.UninstallManager.run_full_scan", return_value=[_ui_app(app_type)]),
        patch("src.ui.screens.uninstall.UninstallSelector.run", return_value=[0]),
        patch("src.ui.screens.uninstall.UninstallPreviewSelector.run", return_value=True),
        patch("src.uninstall.UninstallManager.find_residue_paths", return_value=[]),
        patch("src.uninstall.UninstallManager._candidate_process_names", return_value=[]),
        patch(
            "src.uninstall.UninstallManager.execute_uninstall",
            return_value={"package_removed": True, "removed_paths": []},
        ),
        patch("src.ui.navigator.Navigator.wait_for_return", return_value=False),
        patch("src.ui.screens.uninstall.system.run_command", return_value=MagicMock(ok=True)),
        patch(
            "src.ui.screens.uninstall.system.ensure_sudo_session", return_value=True
        ) as mock_sudo,
    ):
        run_uninstall()

    assert mock_sudo.called is expect_prompt
    assert ("Authorization successful" in capsys.readouterr().out) is expect_prompt


def test_run_uninstall_closes_the_whole_selection_before_removing_any():
    """The SIGTERM grace period is waited through once per run, not once per app:
    ten apps used to spend fifteen seconds staring at a spinner (P4)."""
    order: list[str] = []
    apps = [_ui_app("DNF"), _ui_app("DNF")]

    with (
        patch("src.uninstall.UninstallManager.run_full_scan", return_value=apps),
        patch("src.ui.screens.uninstall.UninstallSelector.run", return_value=[0, 1]),
        patch("src.ui.screens.uninstall.UninstallPreviewSelector.run", return_value=True),
        patch("src.uninstall.UninstallManager.find_residue_paths", return_value=[]),
        patch("src.uninstall.UninstallManager._candidate_process_names", return_value=["test"]),
        patch("src.uninstall.running_process_comms", return_value={"test": [4242]}),
        patch(
            "src.uninstall.UninstallManager.terminate_apps",
            side_effect=lambda targets: order.append(f"terminate:{len(targets)}"),
        ),
        patch(
            "src.uninstall.UninstallManager.execute_uninstall",
            side_effect=lambda app, paths: (
                order.append("remove") or {"package_removed": True, "removed_paths": []}
            ),
        ),
        patch("src.ui.navigator.Navigator.wait_for_return", return_value=False),
        patch("src.ui.screens.uninstall.system.ensure_sudo_session", return_value=True),
        patch("src.ui.screens.uninstall.system.run_command", return_value=MagicMock(ok=True)),
    ):
        run_uninstall()

    assert order == ["terminate:2", "remove", "remove"]


def test_terminate_apps_waits_once_for_the_whole_selection(mock_sleep):
    """SIGTERM everything, wait once, SIGKILL what ignored it, wait once (P2/P4)."""
    mgr = UninstallManager()
    apps = [_ui_app("DNF"), _ui_app("Flatpak")]
    apps[0]["id"], apps[1]["id"] = "editor", "com.example.Player"
    targets = [(apps[0], [], True), (apps[1], [], True)]
    # "player" ignores SIGTERM and is still there on the second pass.
    tables = [
        {"editor": [11], "player": [12], "unrelated": [13]},
        {"player": [12]},
    ]

    with (
        patch("src.uninstall.running_process_comms", side_effect=tables),
        patch(
            "src.uninstall.UninstallManager._candidate_process_names",
            side_effect=[["editor", "editor"], ["player"]],
        ),
        patch("src.uninstall.system.run_command", return_value=MagicMock(ok=True)) as mock_run_cmd,
    ):
        mgr.terminate_apps(targets)

    calls = [call.args[0] for call in mock_run_cmd.call_args_list]
    assert calls == [
        ["flatpak", "kill", "com.example.Player"],
        # Deduplicated: the same comm pattern is not signalled twice.
        ["pkill", "-15", "-x", "editor"],
        ["pkill", "-15", "-x", "player"],
        ["pkill", "-9", "-x", "player"],
    ]
    assert [call.args[0] for call in mock_sleep.call_args_list] == [1.0, 0.5]


def test_terminate_apps_does_not_wait_for_a_kill_it_never_sent(mock_sleep):
    """SIGTERM was enough, so there is nothing to give the kernel time to reap."""
    mgr = UninstallManager()
    with (
        patch("src.uninstall.running_process_comms", side_effect=[{"editor": [11]}, {}]),
        patch("src.uninstall.UninstallManager._candidate_process_names", return_value=["editor"]),
        patch("src.uninstall.system.run_command", return_value=MagicMock(ok=True)) as mock_run_cmd,
    ):
        mgr.terminate_apps([(_ui_app("DNF"), [], True)])

    assert [call.args[0] for call in mock_run_cmd.call_args_list] == [
        ["pkill", "-15", "-x", "editor"]
    ]
    assert [call.args[0] for call in mock_sleep.call_args_list] == [1.0]


def test_terminate_apps_does_not_wait_when_nothing_is_running(mock_sleep):
    """This is what makes the per-app kill step free once terminate_apps has run."""
    mgr = UninstallManager()
    with (
        patch("src.uninstall.running_process_comms", return_value={"unrelated": [1]}),
        patch("src.uninstall.UninstallManager._candidate_process_names", return_value=["editor"]),
        patch("src.uninstall.system.run_command") as mock_run_cmd,
    ):
        mgr.terminate_apps([(_ui_app("DNF"), [], False)])

    assert mock_run_cmd.call_args_list == []
    assert mock_sleep.call_args_list == []


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
    # status<TAB>package<TAB>essential<TAB>priority<TAB>installed-size, the format
    # _DPKG_QUERY_FORMAT asks dpkg-query for.
    mock_run_cmd.return_value = MagicMock(
        ok=True,
        stdout=(
            "ii \tfirefox\tno\toptional\t204800\n"
            "ii \tlinux-image-generic\tno\toptional\t204800\n"
            "ii \tlibreoffice-writer:amd64\tno\toptional\t204800\n"
        ),
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
def test_apt_scan_skips_packages_whose_files_are_gone(mock_which, mock_run_cmd):
    """`dpkg -r` without purge leaves an "rc" row keeping its old Installed-Size.

    Reporting it would promise back space that was freed whenever the package was
    removed, and "uninstalling" it a second time frees nothing (D1).
    """
    mock_which.side_effect = lambda x: "/usr/bin/dpkg-query" if x == "dpkg-query" else None
    mock_run_cmd.return_value = MagicMock(
        ok=True,
        stdout=(
            "ii \tkept-app\tno\toptional\t204800\n"  # installed
            "rc \tghost-app\tno\toptional\t204800\n"  # removed, config files only
            "iU \tunpacked-app\tno\toptional\t204800\n"  # unpacked: files on disk
            "iF \thalf-configured-app\tno\toptional\t204800\n"  # ditto
            "iH \thalf-installed-app\tno\toptional\t204800\n"  # size unreliable
            "in \tnever-installed-app\tno\toptional\t204800\n"
            "i\tno-status-app\tno\toptional\t204800\n"  # too short to read
        ),
    )

    mgr = UninstallManager()
    with (
        patch("src.core.system.get_os_id", return_value="ubuntu"),
        patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")),
    ):
        apps = mgr.run_full_scan()

    assert sorted(app["id"] for app in apps) == [
        "half-configured-app",
        "kept-app",
        "unpacked-app",
    ]


@patch("src.uninstall.system.run_command")
@patch("shutil.which")
def test_apt_scan_trusts_dpkg_over_the_hardcoded_name_lists(mock_which, mock_run_cmd):
    """deb records its own answer to "may this be removed" (D6).

    The name-based guard in _is_system_component was written against Fedora
    package names, so it never matched network-manager or nvidia-dkms-535.
    """
    mock_which.side_effect = lambda x: "/usr/bin/dpkg-query" if x == "dpkg-query" else None
    mock_run_cmd.return_value = MagicMock(
        ok=True,
        stdout=(
            "ii \tbash\tyes\trequired\t204800\n"  # Essential: yes
            "ii \tnetwork-manager\tno\timportant\t204800\n"
            "ii \tinit-system-helpers\tno\trequired\t204800\n"
            # An empty ${Priority} field must not be mistaken for a protected one.
            "ii \tnvidia-dkms-535\tno\t\t204800\n"
            "ii \tordinary-app\tno\toptional\t204800\n"
        ),
    )

    mgr = UninstallManager()
    with (
        patch("src.core.system.get_os_id", return_value="ubuntu"),
        patch("pathlib.Path.home", return_value=Path("/nonexistent_home_for_tests")),
    ):
        apps = mgr.run_full_scan()

    assert sorted(app["id"] for app in apps) == ["nvidia-dkms-535", "ordinary-app"]


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


def test_pre_scan_resolves_each_tool_once_and_skips_an_empty_deb_database(tmp_path, monkeypatch):
    """PATH is searched once per tool, not once per batch, and a box that merely
    has dpkg's tools installed never pays for their query (P3)."""
    monkeypatch.setattr("src.uninstall.RPM_QUERY_BATCH_SIZE", 1)
    # Installing dpkg on a non-deb distro leaves the status database empty, so
    # every `dpkg-query -S` can only answer "no path found".
    monkeypatch.setattr("src.uninstall._DPKG_STATUS_FILE", tmp_path / "dpkg-status")
    (tmp_path / "dpkg-status").write_text("")
    apps_dir = tmp_path / ".local/share/applications"
    apps_dir.mkdir(parents=True)
    for i in range(8):
        (apps_dir / f"app{i}.desktop").write_text(f"[Desktop Entry]\nName=App {i}\n")

    looked_up: list[str] = []

    def which(name):
        looked_up.append(name)
        return f"/usr/bin/{name}" if name in ("rpm", "dpkg-query") else None

    with (
        patch("src.uninstall.shutil.which", side_effect=which),
        patch("pathlib.Path.home", return_value=tmp_path),
        patch(
            "src.uninstall.system.run_command", return_value=MagicMock(ok=True, stdout="")
        ) as mock_run_cmd,
    ):
        UninstallManager()._pre_scan_package_desktop_names()

    assert looked_up.count("rpm") == 1
    assert looked_up.count("dpkg-query") == 1
    assert looked_up.count("pacman") == 1
    tools_run = {call.args[0][0] for call in mock_run_cmd.call_args_list}
    assert tools_run == {"rpm"}
    # Batch size 1 means one command per .desktop file, and the lookups did not
    # grow with them.
    assert len(mock_run_cmd.call_args_list) >= 8
    assert len(looked_up) == 3


def test_pre_scan_queries_dpkg_even_when_rpm_is_also_installed(tmp_path, monkeypatch):
    """The tools are additive: a deb box with rpm installed (for alien, or to
    inspect an .rpm) must still have its .desktop owners looked up, or every APT
    package falls back to the ">100 MB" guess and drops out of the list."""
    monkeypatch.setattr("src.uninstall._DPKG_STATUS_FILE", tmp_path / "dpkg-status")
    (tmp_path / "dpkg-status").write_text("Package: bash\nStatus: install ok installed\n")
    apps_dir = tmp_path / ".local/share/applications"
    apps_dir.mkdir(parents=True)
    (apps_dir / "gimp.desktop").write_text("[Desktop Entry]\nName=GNU Image Manipulation\n")

    def run_command(args, **kwargs):
        if args[0] == "dpkg-query":
            return MagicMock(ok=True, stdout=f"gimp: {apps_dir / 'gimp.desktop'}\n")
        return MagicMock(ok=True, stdout="")

    with (
        patch(
            "src.uninstall.shutil.which",
            side_effect=lambda n: f"/usr/bin/{n}" if n in ("rpm", "dpkg-query") else None,
        ),
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("src.uninstall.system.run_command", side_effect=run_command) as mock_run_cmd,
    ):
        packages, names = UninstallManager()._pre_scan_package_desktop_names()

    tools_run = {call.args[0][0] for call in mock_run_cmd.call_args_list}
    assert tools_run == {"rpm", "dpkg-query"}
    assert "gimp" in packages
    assert names["gimp"] == "GNU Image Manipulation"


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

    # Simulate the app being up: the running check is one /proc pass, not a pgrep
    # per candidate name.
    mock_run.return_value = MagicMock(returncode=0)
    mock_run_cmd.return_value = MagicMock(returncode=0)

    with (
        patch("shutil.which", side_effect=lambda x: "/usr/bin/dnf" if x == "dnf" else None),
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
        patch("src.uninstall.running_process_comms", return_value={"heavy-app": [4242]}),
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
    # The app was reported running, so it must have been signalled: SIGTERM first,
    # then SIGKILL because the second /proc pass still lists it.
    signals = [
        call.args[0][:2] for call in mock_run_cmd.call_args_list if call.args[0][0] == "pkill"
    ]
    assert signals == [["pkill", "-15"], ["pkill", "-9"]]


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
    # apt-get, and with debconf muted: apt warns about its unstable CLI when
    # captured, and a maintainer-script prompt nobody can see would hang the
    # removal until the command timeout (D3). --autoremove takes the dependencies
    # this removal orphans with it, the way -Rns and --clean-deps do (D6).
    mock_run_cmd.assert_any_call(
        ["apt-get", "purge", "--autoremove", "-y", "firefox"],
        use_sudo=True,
        capture=True,
        env=APT_NONINTERACTIVE_ENV,
    )


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


@pytest.mark.parametrize(
    ("os_id", "id_like", "expected_type"),
    [
        ("opensuse-tumbleweed", "", "Zypper"),
        ("sles", "suse", "Zypper"),
        ("fedora", "", "DNF"),
        ("rhel", "fedora", "DNF"),
    ],
)
def test_rpm_scan_labels_suse_packages_zypper(os_id, id_like, expected_type):
    """An rpm distro is not necessarily a dnf distro: openSUSE and SLES ship
    zypper and no dnf, so a "DNF" label sent their removals nowhere (O5)."""
    mgr = UninstallManager()
    with (
        patch("shutil.which", side_effect=lambda x: "/usr/bin/rpm" if x == "rpm" else None),
        patch("src.core.system.get_os_info", return_value=(os_id, id_like)),
        patch(
            "src.uninstall.system.run_command",
            return_value=MagicMock(ok=True, stdout="heavy-app\t150000000\t1700000000\n"),
        ),
    ):
        apps = mgr._scan_rpm_packages(set(), {})

    assert [app["type"] for app in apps] == [expected_type]


@patch("src.core.system.run_command")
def test_execute_uninstall_zypper(mock_run_cmd, test_env):
    """zypper needs --non-interactive for the same reason apt needs debconf muted:
    the prompt would sit invisible behind the spinner until the timeout (O5). And
    --clean-deps because zypper, alone among the four, keeps the dependencies
    nothing needs any more unless it is told to drop them."""
    mgr = UninstallManager()
    app = {"name": "Firefox", "id": "firefox", "type": "Zypper", "size_bytes": 150000000}
    mock_run_cmd.return_value = MagicMock(ok=True)

    with (
        patch("pathlib.Path.home", return_value=test_env),
        patch("src.uninstall.safe_remove", return_value=(True, "OK")),
    ):
        details = mgr.execute_uninstall(app, [])

    assert details["package_removed"] is True
    mock_run_cmd.assert_any_call(
        ["zypper", "--non-interactive", "remove", "--clean-deps", "firefox"],
        use_sudo=True,
        capture=True,
        env=C_LOCALE_ENV,
    )


@patch("src.core.system.run_command")
def test_execute_uninstall_refuses_a_type_it_does_not_know(mock_run_cmd, test_env):
    """The old else ran `dnf remove` for anything unrecognised, which on a zypper
    box was a removal that could not work. Failing says so; guessing does not."""
    mgr = UninstallManager()
    app = {"name": "Mystery", "id": "mystery", "type": "Homebrew", "size_bytes": 10}
    mock_run_cmd.return_value = MagicMock(ok=True)

    with patch("pathlib.Path.home", return_value=test_env):
        details = mgr.execute_uninstall(app, [])

    assert details["package_removed"] is False
    # Nothing was run at all: no `dnf remove` guessed on its behalf.
    assert mock_run_cmd.call_args_list == []


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


def test_run_uninstall_does_not_autoremove_the_whole_machine(capsys):
    """The screen runs no cleanup of its own after the removals (D6).

    It used to end every selection containing an APT app with a system-wide
    `sudo apt-get autoremove --purge -y`, output discarded: one transaction that
    took every auto-installed package nothing needed any more, including ones
    installed long before topo and unrelated to the selection, with no line of it
    in the preview the user had just confirmed. The orphans an app really drags
    out now go in that app's own transaction, which is the one the preview showed.
    """
    mock_apps = [
        {
            "id": "firefox",
            "name": "Firefox",
            "size_bytes": 100,
            "size_str": "100B",
            "type": "APT",
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
            return_value={"package_removed": True, "removed_paths": []},
        ),
        patch("src.core.system.ensure_sudo_session", return_value=True),
        patch("src.ui.navigator.Navigator.wait_for_return", return_value=False),
        patch("src.ui.screens.uninstall.system.run_command") as mock_run_cmd,
    ):
        mock_run_cmd.return_value = MagicMock(ok=True, stdout="")
        run_uninstall()

    # The preview's own simulation is the only apt command this path issues now,
    # and it needs no root: the real removal belongs to execute_uninstall, patched
    # out above, and nothing at all runs after the loop.
    assert [call.args[0] for call in mock_run_cmd.call_args_list] == [
        ["apt-get", "purge", "--autoremove", "-s", "firefox"]
    ]
    assert not any(call.kwargs.get("use_sudo") for call in mock_run_cmd.call_args_list)
    assert "Removed 1 app(s)" in capsys.readouterr().out


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


def test_cjk_font_bundles_are_never_offered_for_removal():
    """One 100 MB+ font package with no .desktop file used to fall through to the
    "anything this big is a user app" rule, and removing it turns every CJK glyph
    on the desktop into a box (D5)."""
    mgr = UninstallManager()
    for pkg in (
        "fonts-noto-cjk",  # deb
        "google-noto-sans-cjk-fonts",  # Fedora
        "noto-fonts-cjk",  # Arch
        "fonts-droid-fallback",
    ):
        assert mgr._is_system_component(pkg, pkg), pkg
    # Font *applications* are ordinary user apps: neither name has a whole
    # "fonts" segment.
    for pkg in ("fontforge", "font-manager", "fontbase"):
        assert not mgr._is_system_component(pkg, pkg), pkg


def test_dpkg_install_time_prefers_the_arch_qualified_list(tmp_path, monkeypatch):
    """More than half of /var/lib/dpkg/info on a stock ubuntu:24.04 is named
    `pkg:arch.list`; missing that name loses the timestamp the "recently
    installed first" ordering depends on (D2)."""
    monkeypatch.setattr("src.uninstall._DPKG_INFO_DIR", tmp_path)
    mgr = UninstallManager()

    qualified = tmp_path / "libacl1:amd64.list"
    qualified.write_text("/usr/lib\n")
    os.utime(qualified, (1_700_000_000, 1_700_000_000))
    assert mgr._dpkg_install_time("libacl1:amd64", "libacl1") == 1_700_000_000

    plain = tmp_path / "firefox.list"
    plain.write_text("/usr/bin/firefox\n")
    os.utime(plain, (1_600_000_000, 1_600_000_000))
    assert mgr._dpkg_install_time("firefox", "firefox") == 1_600_000_000

    assert mgr._dpkg_install_time("not-installed:amd64", "not-installed") == 0


@patch("src.uninstall.shutil.which")
@patch("src.uninstall.system.run_command")
def test_apt_scan_dates_a_multi_arch_package_from_its_qualified_list(
    mock_run_cmd, mock_which, tmp_path, monkeypatch
):
    """The scan has to hand dpkg-query's own name to the lookup before the stripped
    one, or a Multi-Arch package sorts as if it had never been installed (D2)."""
    monkeypatch.setattr("src.uninstall._DPKG_INFO_DIR", tmp_path)
    list_file = tmp_path / "firefox:amd64.list"
    list_file.write_text("/usr/bin/firefox\n")
    os.utime(list_file, (1_700_000_000, 1_700_000_000))

    mock_which.side_effect = lambda n: "/usr/bin/dpkg-query" if n == "dpkg-query" else None
    mock_run_cmd.return_value = MagicMock(
        ok=True, stdout="ii \tfirefox:amd64\tno\toptional\t204800\n"
    )

    apps = UninstallManager()._scan_apt_packages({"firefox"}, {})

    assert [(app["id"], app["install_time"]) for app in apps] == [("firefox", 1_700_000_000)]


def test_pre_scan_reads_every_owner_of_a_shared_desktop_path(tmp_path, monkeypatch):
    """dpkg-query -S puts all owners of a path on one comma-separated line and an
    owner may carry its own :arch, so the path is what follows the LAST ": " (D4)."""
    monkeypatch.setattr("src.uninstall._DPKG_STATUS_FILE", tmp_path / "dpkg-status")
    (tmp_path / "dpkg-status").write_text("Package: bash\n")
    apps_dir = tmp_path / ".local/share/applications"
    apps_dir.mkdir(parents=True)
    desktop = apps_dir / "shared.desktop"
    desktop.write_text("[Desktop Entry]\nName=Shared App\nExec=shared\n")

    mgr = UninstallManager()
    with (
        patch(
            "src.uninstall.shutil.which",
            side_effect=lambda n: "/usr/bin/dpkg-query" if n == "dpkg-query" else None,
        ),
        patch("pathlib.Path.home", return_value=tmp_path),
        patch(
            "src.uninstall.system.run_command",
            return_value=MagicMock(ok=True, stdout=f"procps, libc6:amd64, bash: {desktop}\n"),
        ),
    ):
        packages, names = mgr._pre_scan_package_desktop_names()

    assert packages == {"procps", "libc6", "bash"}
    assert names == {"procps": "Shared App", "libc6": "Shared App", "bash": "Shared App"}


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


def test_a_scanner_asks_no_question_of_a_database_that_holds_nothing(monkeypatch, tmp_path):
    """Having the tools is not having the packages (D9).

    Installing `alien` drags dpkg-query onto Fedora and rpm onto Debian, and both
    leave behind a database that can only ever answer nothing. Each of those is a
    fork with a 60-second timeout on every full scan. The module already asked
    this question before its `dpkg-query -S` batches; now the two -qa/-W scanners
    ask it too, on both sides.
    """
    mgr = UninstallManager()
    empty_status = tmp_path / "dpkg-status"
    empty_status.write_text("")
    empty_rpm_db = tmp_path / "rpmdb"
    empty_rpm_db.mkdir()
    monkeypatch.setattr("src.uninstall._DPKG_STATUS_FILE", empty_status)
    monkeypatch.setattr("src.uninstall._RPM_DB_DIR", empty_rpm_db)

    with (
        patch("src.uninstall.shutil.which", side_effect=lambda tool: f"/usr/bin/{tool}"),
        patch("src.uninstall.system.run_command") as run_command,
    ):
        assert mgr._scan_apt_packages(set(), {}) == []
        assert mgr._scan_rpm_packages(set(), {}) == []
        # Not "returned nothing": never ran.
        assert run_command.call_args_list == []

    # A directory holding only an empty file is still empty, and a missing one is
    # not an error to report -- both are just "no rpm packages here".
    (empty_rpm_db / "Packages").write_bytes(b"")
    assert _has_rpm_database() is False
    monkeypatch.setattr("src.uninstall._RPM_DB_DIR", tmp_path / "absent")
    assert _has_rpm_database() is False


def test_a_populated_database_is_worth_asking(package_databases):
    """The other half of D9's guard: a real box must still be scanned.

    The rpm check is deliberately loose -- any non-empty file counts, because the
    filename depends on the backend (sqlite/bdb/ndb) and a guard that listed them
    would silently drop every RPM app the day a fourth appears.
    """
    assert _has_deb_database() is True
    assert _has_rpm_database() is True
    for leftover in package_databases.rpm.iterdir():
        leftover.unlink()
    (package_databases.rpm / "backend-nobody-has-heard-of").write_bytes(b"\x00")
    assert _has_rpm_database() is True


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


def test_build_removal_targets_reuses_the_scan_index(monkeypatch):
    """Without this the preview re-walks the search roots and fully recurses the
    icon directories once per selected app (P1)."""
    mgr = UninstallManager()
    index = {Path("/nonexistent-root"): object()}
    monkeypatch.setattr(mgr, "_current_scan_cache_key", lambda: ("key",))
    monkeypatch.setattr(mgr, "_pre_scan_package_desktop_names", lambda: (set(), {}))
    monkeypatch.setattr(mgr, "_pre_scan_search_roots", lambda: index)
    monkeypatch.setattr(mgr, "_calculate_app_sizes_and_residues", lambda apps, roots: None)
    for name in (
        "_scan_rpm_packages",
        "_scan_apt_packages",
        "_scan_pacman_packages",
        "_scan_snap_apps",
    ):
        monkeypatch.setattr(mgr, name, lambda *_: [])
    for name in ("_scan_flatpak_apps", "_scan_npm_global_packages", "_scan_standalone_cli_apps"):
        monkeypatch.setattr(mgr, name, lambda: [])

    mgr.run_full_scan()
    assert mgr._pre_scanned_entries is index

    seen: list[object] = []

    def record(app_id, app_name, pre_scanned_entries=None):
        seen.append(pre_scanned_entries)
        return []

    app = {"id": "tool", "name": "Tool", "type": "NPM", "size_bytes": 10}
    with (
        patch.object(mgr, "find_residue_paths", side_effect=record),
        patch.object(mgr, "_candidate_process_names", return_value=[]),
    ):
        mgr.build_removal_targets([app, app])

    assert seen == [index, index]

    # A manager that never scanned still has to work; it just pays for the walk.
    fresh = UninstallManager()
    with (
        patch.object(fresh, "find_residue_paths", side_effect=record),
        patch.object(fresh, "_candidate_process_names", return_value=[]),
    ):
        fresh.build_removal_targets([app])
    assert seen[-1] is None


def _collateral(app_type: str, stdout: str, app_id: str = "vlc"):
    """Run one collateral query against a canned reply, returning (names, argv, env)."""
    mgr = UninstallManager()
    with patch(
        "src.uninstall.system.run_command", return_value=MagicMock(stdout=stdout)
    ) as mock_run_cmd:
        names = mgr._collateral_packages({"id": app_id, "type": app_type})
    if not mock_run_cmd.call_args_list:
        return names, None, None
    call = mock_run_cmd.call_args
    return names, call.args[0], call.kwargs.get("env")


def test_collateral_packages_reads_an_apt_simulation():
    """Ticking one small entry can drag out half a desktop, and the preview said
    nothing about it. -s simulates the real transaction without needing root (O4).

    --autoremove is part of that transaction (D6): the packages the removal
    orphans are narrated as Purg lines here, and without the flag apt would list
    them in prose the parser is right to ignore -- which is how the preview came
    to omit exactly what the removal then took.
    """
    names, argv, env = _collateral(
        "APT",
        "Reading package lists...\n"
        "The following packages will be REMOVED:\n"
        "Remv vlc [3:3.0.20-3]\n"
        "Purg vlc-plugin-base [3:3.0.20-3]\n"
        "Remv libvlc-bin:i386 [3:3.0.20-3]\n"
        "Remv libvlc-bin:i386 [3:3.0.20-3]\n"
        "Inst libfoo [1.0] (1.1 Ubuntu:24.04 [amd64])\n",
    )

    # The app itself is dropped -- including when apt narrates it qualified --
    # duplicates collapse, and Inst/prose lines are not removals.
    assert names == ["vlc-plugin-base", "libvlc-bin"]
    assert argv == ["apt-get", "purge", "--autoremove", "-s", "vlc"]
    assert env == APT_NONINTERACTIVE_ENV


def test_collateral_packages_previews_the_apt_transaction_that_will_run():
    """The preview query and the removal differ by -s versus -y, nothing else.

    That is the whole of D6: the screen used to follow the removals with one
    system-wide `apt-get autoremove --purge -y`, so what came off the machine was
    a superset of what the preview had listed. Any flag added to one side of this
    pair and not the other reopens the gap.
    """
    mgr = UninstallManager()
    app = {"id": "vlc", "name": "VLC", "type": "APT", "size_bytes": 0}

    with patch("src.uninstall.system.run_command", return_value=MagicMock(stdout="")) as query:
        mgr._collateral_packages(app)
    with (
        patch("src.uninstall.system.run_command", return_value=MagicMock(ok=True)) as removal,
        patch("src.uninstall.record_deletion_audit"),
        patch("src.uninstall.record_history_session"),
        patch("src.uninstall.running_process_comms", return_value=set()),
    ):
        mgr.execute_uninstall(app, [])

    preview_argv = query.call_args.args[0]
    removal_argv = next(
        call.args[0] for call in removal.call_args_list if call.args[0][0] == "apt-get"
    )
    assert preview_argv == ["apt-get", "purge", "--autoremove", "-s", "vlc"]
    assert removal_argv == ["apt-get", "purge", "--autoremove", "-y", "vlc"]


def test_collateral_packages_asks_pacman_to_print_instead_of_removing():
    """--print-format is what lets this run without the database lock, so the
    preview can be drawn before the password is asked for (O4)."""
    names, argv, env = _collateral("Pacman", "vlc\nqt5-base\nlibvlc\n")

    assert names == ["qt5-base", "libvlc"]
    assert argv == ["pacman", "-Rns", "--print-format", "%n", "vlc"]
    assert env == C_LOCALE_ENV


@pytest.mark.parametrize("dnf_binary", ["dnf5", "dnf"])
def test_collateral_packages_asks_dnf_what_requires_the_package(dnf_binary):
    """Every exact dnf dry-run wants the database lock, so the question becomes
    "what requires this" -- a first level rather than the full closure (O4)."""
    with patch("shutil.which", side_effect=lambda x: f"/usr/bin/{x}" if x == dnf_binary else None):
        names, argv, env = _collateral("DNF", "vlc\nvlc-plugins-freeworld\n")

    assert names == ["vlc-plugins-freeworld"]
    assert argv == [
        dnf_binary,
        "repoquery",
        "-C",
        "--installed",
        "--whatrequires",
        "vlc",
        "--qf",
        "%{name}\n",
    ]
    assert env == C_LOCALE_ENV


def test_collateral_packages_reads_rpm_on_a_zypper_box_and_ignores_its_prose():
    """rpm answers "no package requires X" on stdout and exits 1, so the reply is
    filtered by shape -- a package name never holds a space (O4)."""
    names, argv, env = _collateral("Zypper", "no package requires vlc\n")

    assert names == []
    assert argv == ["rpm", "-q", "--whatrequires", "vlc", "--qf", "%{NAME}\n"]
    assert env == C_LOCALE_ENV


@pytest.mark.parametrize("app_type", ["Flatpak", "Snap", "NPM", "CLI"])
def test_collateral_packages_asks_nothing_for_a_removal_that_takes_nothing(app_type):
    """A Flatpak, Snap, NPM or CLI removal has no reverse dependencies to report,
    so it must not cost a fork per selected app either."""
    names, argv, _env = _collateral(app_type, "should not be read")

    assert names == []
    assert argv is None


def test_collateral_packages_survives_a_query_that_fails():
    """run_command turns a missing binary or a timeout into a result rather than
    raising; the preview then says nothing, exactly as it did before (O4)."""
    mgr = UninstallManager()
    with patch(
        "src.uninstall.system.run_command",
        return_value=MagicMock(returncode=127, stdout="", ok=False),
    ):
        assert mgr._collateral_packages({"id": "vlc", "type": "DNF"}) == []
    # An entry with no package id has nothing to ask about.
    assert mgr._collateral_packages({"id": "", "type": "DNF"}) == []


def test_build_removal_targets_records_the_collateral_for_every_app():
    """The preview reads it off the app dict, so the tuple keeps its three fields
    and every selected app carries a list -- empty when nothing comes with it."""
    mgr = UninstallManager()
    apps = [
        {"id": "vlc", "name": "VLC", "type": "DNF", "size_bytes": 10},
        {"id": "com.example.App", "name": "Example", "type": "Flatpak", "size_bytes": 10},
    ]

    with (
        patch.object(mgr, "find_residue_paths", return_value=[]),
        patch.object(mgr, "_candidate_process_names", return_value=[]),
        patch.object(
            mgr,
            "_collateral_packages",
            side_effect=lambda app: ["vlc-plugins"] if app["type"] == "DNF" else [],
        ),
    ):
        targets = mgr.build_removal_targets(apps)

    assert [len(target) for target in targets] == [3, 3]
    assert [app["collateral_packages"] for app, _paths, _running in targets] == [
        ["vlc-plugins"],
        [],
    ]


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


def test_run_uninstall_reports_apps_already_removed_before_a_ctrl_c(capsys):
    """Ctrl-C mid-selection still says which apps are gone (I2).

    removed_names/failed_names were discarded along with the exception, so a user
    who interrupted a five-app removal after two had been purged saw one line --
    "Process interrupted by user." -- and had no way to tell which two. The report
    now runs from the loop's `finally`, and says the rest never started.
    """
    mock_apps = [
        {
            "id": f"app{i}",
            "name": name,
            "size_bytes": 1024,
            "size_str": "1.0 KiB",
            "type": "DNF",
            "install_time": 0,
        }
        for i, name in enumerate(("Firefox", "Chromium"))
    ]
    results = [
        {"package_removed": True, "removed_paths": []},
        KeyboardInterrupt(),
    ]

    with (
        patch("src.uninstall.UninstallManager.run_full_scan", return_value=mock_apps),
        patch("src.ui.screens.uninstall.UninstallSelector.run", return_value=[0, 1]),
        patch("src.ui.screens.uninstall.UninstallPreviewSelector.run", return_value=True),
        patch("src.uninstall.UninstallManager.find_residue_paths", return_value=[]),
        patch("src.uninstall.UninstallManager.execute_uninstall", side_effect=results),
        patch("src.core.system.ensure_sudo_session", return_value=True),
        patch("src.ui.screens.uninstall.ScanCache.clear") as clear_cache,
        patch("src.ui.screens.uninstall.play_delete") as play,
        patch("src.ui.navigator.Navigator.wait_for_return", return_value=False),
        pytest.raises(KeyboardInterrupt),
    ):
        run_uninstall()

    out = capsys.readouterr().out
    assert "Removed Firefox" in out
    assert "Chromium" not in out
    assert "Uninstall interrupted" in out
    assert "Removed 1 app(s)" in out
    assert "left untouched" in out
    # The scan cache is stale either way -- one app really is gone.
    clear_cache.assert_called_once_with()
    # No success chime for a run the user stopped.
    play.assert_not_called()
