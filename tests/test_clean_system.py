from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.clean.system import (
    DryRunReporter,
    clean_journal,
    clean_old_kernels,
    clean_orphaned_packages,
    clean_package_manager,
    clean_rotated_logs,
    clean_snaps,
    clean_system_data,
    clean_zombies,
)
from src.core.system import APT_NONINTERACTIVE_ENV, C_LOCALE_ENV


def test_package_cache_paths_cover_supported_managers(tmp_path, monkeypatch):
    from src.clean import system as module

    apt = tmp_path / "apt"
    (apt / "partial").mkdir(parents=True)
    monkeypatch.setattr(module.Path, "exists", lambda self: True)
    assert [str(p) for p in module._get_package_manager_cache_paths("apt")] == [
        "/var/cache/apt/archives",
        "/var/cache/apt/archives/partial",
    ]
    assert module._get_package_manager_cache_paths("unknown") == []


@patch("shutil.which", return_value=None)
def test_system_cleaners_no_tools_return_zero(_mock_which):
    assert clean_snaps() == (0, 0, 0)
    assert clean_journal() == (0, 0, 0)


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_orphaned_packages_fedora(mock_get_os_id, mock_run, mock_which):
    mock_get_os_id.return_value = "fedora"
    mock_which.side_effect = lambda x: "/usr/bin/dnf" if x == "dnf" else None
    mock_run.return_value = MagicMock(returncode=0, stdout="Removed 500 MB\nPackage1\nPackage2")

    s, i, c = clean_orphaned_packages(dry_run=False)
    assert s > 0
    assert i > 0
    assert c == 1
    mock_run.assert_called_with(
        ["dnf", "autoremove", "-y"], use_sudo=True, capture=True, env=C_LOCALE_ENV
    )

    s, i, c = clean_orphaned_packages(dry_run=True)
    assert c == 1


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_orphaned_packages_ubuntu(mock_get_os_id, mock_run, mock_which):
    mock_get_os_id.return_value = "ubuntu"
    mock_which.return_value = "/usr/bin/apt-get"
    mock_run.return_value = MagicMock(
        returncode=0, stdout="After this operation, 50.0 MB of additional disk space will be freed."
    )

    s, i, c = clean_orphaned_packages(dry_run=False)
    assert s > 0
    assert i == 1
    assert c == 1
    # apt-get, unlike dnf, runs maintainer scripts that may ask debconf a question
    # with nobody able to see the prompt.
    mock_run.assert_called_with(
        ["apt-get", "autoremove", "-y"],
        use_sudo=True,
        capture=True,
        env=APT_NONINTERACTIVE_ENV,
    )


@patch("src.clean.system.run_command")
def test_clean_zombies(mock_run):
    # Mock ps output with a zombie process
    mock_run.return_value = MagicMock(
        returncode=0,
        ok=True,
        stdout="S   PID  PPID COMMAND\nZ   1234  1111 defunct-app\nS   5678  2222 normal-app\n",
    )

    s, i, c = clean_zombies(dry_run=False)
    assert i == 1
    assert c == 1
    # Check that SIGCHLD was sent to the parent (1111)
    mock_run.assert_any_call(["kill", "-SIGCHLD", "1111"], use_sudo=True, capture=True)

    # Test dry run
    s, i, c = clean_zombies(dry_run=True)
    assert c == 1


@patch("src.clean.system.run_command")
def test_clean_zombies_never_signals_init_or_pid_zero(mock_run):
    """Numeric comparison, not string membership: "01"/"０１" must not reach init (L-2)."""
    mock_run.return_value = MagicMock(
        returncode=0,
        ok=True,
        stdout=(
            "Z   1234  1 a\n"
            "Z   1235  0 b\n"
            "Z   1236  01 c\n"
            "Z   1237  001 d\n"
            "Z   1238  ０１ e\n"
            "Z   1239  4321 f\n"
        ),
    )

    s, i, c = clean_zombies(dry_run=False)

    assert i == 6
    signalled = [call.args[0][2] for call in mock_run.call_args_list if call.args[0][0] == "kill"]
    assert signalled == ["4321"]


@patch("shutil.which")
@patch("src.clean.system.run_command")
def test_clean_snaps(mock_run, mock_which):
    mock_which.return_value = "/usr/bin/snap"
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="Name  Version  Rev   Tracking  Publisher   Notes\ncore22  2023  1234  latest    canonical*  disabled\n",
    )

    s, i, c = clean_snaps(dry_run=False)
    assert i == 1
    assert c == 1
    mock_run.assert_any_call(
        ["snap", "remove", "core22", "--revision", "1234"], use_sudo=True, capture=True
    )

    s, i, c = clean_snaps(dry_run=True)
    assert c == 1


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_package_manager_fedora(mock_get_os_id, mock_run, mock_which):
    mock_get_os_id.return_value = "fedora"
    mock_which.side_effect = lambda x: "/usr/bin/dnf" if x == "dnf" else None
    mock_run.return_value = MagicMock(returncode=0, stdout="freed 100 MB")

    s, i, c = clean_package_manager(dry_run=False)
    assert i == 1
    assert c == 1
    mock_run.assert_called_with(
        ["dnf", "clean", "packages"], use_sudo=True, capture=True, env=C_LOCALE_ENV
    )

    s, i, c = clean_package_manager(dry_run=True)
    assert c == 1


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_package_manager_ubuntu(mock_get_os_id, mock_run, mock_which):
    mock_get_os_id.return_value = "ubuntu"
    mock_which.side_effect = lambda x: "/usr/bin/apt-get" if x == "apt-get" else None
    mock_run.return_value = MagicMock(returncode=0, stdout="")

    s, i, c = clean_package_manager(dry_run=False)
    assert i == 1
    assert c == 1
    mock_run.assert_any_call(["apt-get", "clean"], use_sudo=True, capture=True, env=C_LOCALE_ENV)


@patch("shutil.which")
@patch("src.clean.system.run_command")
def test_clean_journal(mock_run, mock_which):
    mock_which.return_value = "/usr/bin/journalctl"
    mock_run.return_value = MagicMock(returncode=0, stdout="freed 200 MB")

    s, i, c = clean_journal(dry_run=False)
    assert s > 0
    assert i == 1
    assert c == 1
    mock_run.assert_called_with(
        ["journalctl", "--vacuum-size=1M"], use_sudo=True, capture=True, env=C_LOCALE_ENV
    )

    s, i, c = clean_journal(dry_run=True)
    assert c == 1


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_package_manager_ubuntu_includes_snap_stats(mock_get_os_id, mock_run, mock_which):
    """Snap revision removals must be counted in package-manager stats."""
    mock_get_os_id.return_value = "ubuntu"
    mock_which.side_effect = lambda x: f"/usr/bin/{x}" if x in ("apt-get", "snap") else None

    def run_side_effect(cmd, **kwargs):
        if cmd[:2] == ["snap", "list"]:
            return MagicMock(
                returncode=0,
                ok=True,
                stdout="Name Version Rev Tracking Publisher Notes\n"
                "core22 2023 1234 latest canonical* disabled\n",
            )
        return MagicMock(returncode=0, ok=True, stdout="")

    mock_run.side_effect = run_side_effect

    s, i, c = clean_package_manager(dry_run=False)
    # apt cache (1 item / 1 cat) + one removed snap revision (1 item / 1 cat)
    assert i == 2
    assert c == 2


def test_dry_run_reporter_empty_and_items_only(capsys):
    assert DryRunReporter.report("nothing") == (0, 0, 0)
    assert DryRunReporter.report("items", items_count=2, dry_run=True) == (0, 2, 1)
    assert "(2 items) would be cleaned" in capsys.readouterr().out


def test_snap_empty_and_malformed_output():
    with (
        patch("shutil.which", return_value="/usr/bin/snap"),
        patch("src.clean.system.run_command", return_value=SimpleNamespace(ok=True, stdout="")),
    ):
        assert clean_snaps() == (0, 0, 0)
    result = SimpleNamespace(ok=True, stdout="Name Version Rev Notes\nshort disabled\n")
    with (
        patch("shutil.which", return_value="/usr/bin/snap"),
        patch("src.clean.system.run_command", return_value=result),
    ):
        assert clean_snaps() == (0, 0, 0)


def test_package_cache_paths_dnf_and_pacman(monkeypatch):
    from src.clean import system as module

    existing = {"/var/cache/dnf5daemon-server", "/var/cache/pacman/pkg"}
    monkeypatch.setattr(module.Path, "exists", lambda self: str(self) in existing)
    assert [str(p) for p in module._get_package_manager_cache_paths("dnf")] == [
        "/var/cache/dnf5daemon-server"
    ]
    assert [str(p) for p in module._get_package_manager_cache_paths("pacman")] == [
        "/var/cache/pacman/pkg"
    ]


def test_package_manager_dnf5_measured_and_failed_paths():
    with (
        patch("src.clean.system.get_os_id", return_value="fedora"),
        patch(
            "shutil.which", side_effect=lambda n: "/usr/bin/dnf5" if n in ("dnf", "dnf5") else None
        ),
        patch("src.clean.system._get_package_manager_cache_paths", return_value=[]),
        patch("src.clean.system._measure_package_cache_size", side_effect=[100, 20]),
        patch(
            "src.clean.system.run_command", return_value=SimpleNamespace(ok=True, stdout="")
        ) as run,
    ):
        assert clean_package_manager() == (80, 1, 1)
        run.assert_called_once_with(
            ["dnf5", "clean", "packages"], use_sudo=True, capture=True, env=C_LOCALE_ENV
        )
    with (
        patch("src.clean.system.get_os_id", return_value="fedora"),
        patch("shutil.which", side_effect=lambda n: "/usr/bin/dnf" if n == "dnf" else None),
        patch("src.clean.system._get_package_manager_cache_paths", return_value=[]),
        patch("src.clean.system._measure_package_cache_size", side_effect=[0, 0]),
        patch("src.clean.system.run_command", return_value=SimpleNamespace(ok=False, stdout="")),
    ):
        assert clean_package_manager() == (0, 0, 0)


def test_journal_failure_or_zero_output_is_noop():
    with (
        patch("shutil.which", return_value="/usr/bin/journalctl"),
        patch("src.clean.system.run_command", return_value=SimpleNamespace(ok=False, stdout="")),
    ):
        assert clean_journal() == (0, 0, 0)


def test_orphaned_pacman_paths():
    with (
        patch("src.clean.system.get_os_id", return_value="arch"),
        patch("shutil.which", side_effect=lambda n: "/usr/bin/pacman" if n == "pacman" else None),
        patch(
            "src.clean.system.run_command",
            return_value=SimpleNamespace(ok=True, stdout="foo\nbar\n"),
        ) as run,
    ):
        assert clean_orphaned_packages(dry_run=True) == (0, 0, 1)
        assert clean_orphaned_packages() == (0, 2, 1)
        assert run.call_args_list[-1].args[0] == ["pacman", "-Rns", "--noconfirm", "foo", "bar"]


def test_zombies_failure_and_empty_output():
    with patch("src.clean.system.run_command", return_value=SimpleNamespace(ok=False, stdout="")):
        assert clean_zombies() == (0, 0, 0)
    with patch(
        "src.clean.system.run_command",
        return_value=SimpleNamespace(ok=True, stdout="S pid ppid cmd\n"),
    ):
        assert clean_zombies() == (0, 0, 0)


def test_old_kernels_debian_and_dnf_paths():
    deb = "ii linux-image-5.10.1 x\nii linux-image-5.10.2 x\nii linux-image-6.1.0 x\n"
    with (
        patch("src.clean.system.platform.release", return_value="6.1.0-generic"),
        patch("shutil.which", side_effect=lambda n: "/usr/bin/" + n),
        patch("src.clean.system.run_command", return_value=SimpleNamespace(ok=True, stdout=deb)),
    ):
        assert clean_old_kernels(dry_run=True) == (0, 0, 1)
        assert clean_old_kernels() == (0, 1, 1)
    rpm = "5.10.1-1\n5.10.2-1\n6.1.0-1\n"
    with (
        patch("src.clean.system.platform.release", return_value="6.1.0-1"),
        patch("shutil.which", side_effect=lambda n: "/usr/bin/dnf" if n == "dnf" else None),
        patch("src.clean.system.run_command", return_value=SimpleNamespace(ok=True, stdout=rpm)),
    ):
        assert clean_old_kernels(dry_run=True) == (0, 0, 1)
        assert clean_old_kernels() == (0, 1, 1)


def test_rotated_logs_and_system_aggregation(tmp_path, monkeypatch):
    (tmp_path / "old.log.1").write_bytes(b"abc")
    monkeypatch.setattr("src.clean.system.Path", lambda _: tmp_path)
    with (
        patch("src.clean.system.get_size_fast", return_value=3),
        patch("src.clean.system.safe_remove", return_value=(True, 3)),
    ):
        assert clean_rotated_logs() == (3, 1, 1)
    values = [(1, 2, 3)] * 6
    with patch.multiple(
        "src.clean.system",
        clean_package_manager=lambda _: values[0],
        clean_orphaned_packages=lambda _: values[0],
        clean_old_kernels=lambda _: values[0],
        clean_journal=lambda _: values[0],
        clean_rotated_logs=lambda _: values[0],
        clean_zombies=lambda _: values[0],
    ):
        assert clean_system_data(True) == (6, 12, 18)
