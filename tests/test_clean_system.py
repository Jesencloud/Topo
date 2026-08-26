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


def test_package_cache_paths_cover_supported_managers(monkeypatch):
    from src.clean import system as module

    monkeypatch.setattr(module.Path, "exists", lambda self: True)
    # One path per family, taken from the table Analyze reads: apt's own
    # `partial/` subdirectory is not listed, because get_size_fast() recurses into
    # it and naming it separately counted its bytes twice.
    assert [str(p) for p in module._get_package_manager_cache_paths("apt")] == [
        "/var/cache/apt/archives"
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

    existing = {
        "/var/cache/dnf",
        "/var/cache/dnf5daemon-server",
        "/var/cache/pacman/pkg",
        "/var/cache/zypp/packages",
    }
    monkeypatch.setattr(module.Path, "exists", lambda self: str(self) in existing)
    # Both dnf caches are measured, not just the first found: dnf5 moved the cache
    # and leaves the old directory behind, and one `clean packages` empties both.
    assert [str(p) for p in module._get_package_manager_cache_paths("dnf")] == [
        "/var/cache/dnf",
        "/var/cache/dnf5daemon-server",
    ]
    assert [str(p) for p in module._get_package_manager_cache_paths("pacman")] == [
        "/var/cache/pacman/pkg"
    ]
    assert [str(p) for p in module._get_package_manager_cache_paths("zypper")] == [
        "/var/cache/zypp/packages"
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


# Real `dpkg -l 'linux-image-*'` output, header and all. The row order is dpkg's
# own -- alphabetical, which puts "-100-" ahead of "-31-" and the metapackage
# last -- because the code under test must not read it as a version order.
_UBUNTU_KERNEL_ROWS = """\
Desired=Unknown/Install/Remove/Purge/Hold
| Status=Not/Inst/Conf-files/Unpacked/halF-conf/Half-inst/trig-aWait/Trig-pend
|/ Err?=(none)/Reinst-required (Status,Err: uppercase=bad)
||/ Name                          Version       Architecture Description
+++-=============================-=============-============-=================
ii  linux-image-6.8.0-100-generic 6.8.0-100.100 amd64        Signed kernel image
ii  linux-image-6.8.0-31-generic  6.8.0-31.31   amd64        Signed kernel image
ii  linux-image-6.8.0-45-generic  6.8.0-45.45   amd64        Signed kernel image
ii  linux-image-generic           6.8.0.100.100 amd64        Generic metapackage
"""
_DEBIAN_KERNEL_ROWS = """\
ii  linux-image-5.10.0-26-amd64 5.10.197-1 amd64 Linux 5.10 for 64-bit PCs
ii  linux-image-5.10.0-28-amd64 5.10.209-2 amd64 Linux 5.10 for 64-bit PCs
ii  linux-image-6.1.0-18-amd64  6.1.76-1   amd64 Linux 6.1 for 64-bit PCs
ii  linux-image-amd64           6.1.76-1   amd64 Linux for 64-bit PCs (meta)
"""
# What `apt-get purge -y` prints. The "2 to remove" line is kept verbatim on
# purpose: parse_size_from_text() reads it as 2 TB, which is why the code has its
# own parser anchored on apt's sentence.
_APT_PURGE_OUTPUT = """\
Reading package lists...
The following packages will be REMOVED:
  linux-image-6.8.0-31-generic*
0 upgraded, 0 newly installed, 2 to remove and 0 not upgraded.
After this operation, 312 MB disk space will be freed.
Removing linux-image-6.8.0-31-generic (6.8.0-31.31) ...
"""


_PURGE_OK = SimpleNamespace(ok=True, stdout="")


def _purge_kernels(rows, os_id, release, purge=_PURGE_OK, dry_run=False):
    """clean_old_kernels() over a `dpkg -l` listing; returns (purged, result)."""
    purged = []

    def fake_run_command(args, **kwargs):
        if args[:2] == ["dpkg", "-l"]:
            return SimpleNamespace(ok=True, stdout=rows)
        purged.append(args)
        return purge

    with (
        patch("src.clean.system.get_os_id", return_value=os_id),
        patch("src.clean.system.platform.release", return_value=release),
        patch("shutil.which", side_effect=lambda n: "/usr/bin/" + n),
        patch("src.clean.system.run_command", side_effect=fake_run_command),
    ):
        return purged, clean_old_kernels(dry_run=dry_run)


def test_old_kernels_on_ubuntu_purges_the_oldest_not_the_newest():
    """Every Ubuntu kernel package ends in -generic, and the whole cleaner used to
    write them all off as metapackages, so `topo clean` silently did nothing.

    The surviving pair also has to be the *newest* two: dpkg lists -100- before
    -31-, so keeping "the last row" kept the oldest kernel and purged the rest.
    """
    purged, result = _purge_kernels(_UBUNTU_KERNEL_ROWS, "ubuntu", "6.8.0-45-generic")

    assert purged == [["apt-get", "purge", "-y", "linux-image-6.8.0-31-generic"]]
    assert result == (0, 1, 1)

    dry_purged, dry_result = _purge_kernels(
        _UBUNTU_KERNEL_ROWS, "ubuntu", "6.8.0-45-generic", dry_run=True
    )

    assert dry_purged == []
    assert dry_result == (0, 0, 1)


def test_old_kernels_on_debian_leaves_a_kernel_to_fall_back_to():
    """Debian's metapackage is linux-image-amd64: no -generic suffix, and last in
    dpkg's alphabetical order, so it used to take the "keep one previous kernel"
    slot while both real fallbacks were purged -- leaving nothing but the running
    kernel in GRUB.
    """
    purged, result = _purge_kernels(_DEBIAN_KERNEL_ROWS, "debian", "6.1.0-18-amd64")

    # Three of the four rows survive: the metapackage that pulls in future
    # kernels, the running kernel, and one bootable fallback.
    assert purged == [["apt-get", "purge", "-y", "linux-image-5.10.0-26-amd64"]]
    assert result == (0, 1, 1)


def test_old_kernels_will_not_purge_the_last_fallback():
    # The same listing without its oldest kernel: running, one fallback, and the
    # metapackage, so there is nothing left to remove.
    rows = "\n".join(_DEBIAN_KERNEL_ROWS.splitlines()[1:]) + "\n"

    purged, result = _purge_kernels(rows, "debian", "6.1.0-18-amd64")

    assert purged == []
    assert result == (0, 0, 0)


def test_old_kernels_reports_the_bytes_apt_says_it_freed():
    # apt counts in decimal MB (apt-pkg divides by 1000), so 312 MB is 312e6 --
    # not the 327e6 a binary reading would report.
    purged, result = _purge_kernels(
        _UBUNTU_KERNEL_ROWS,
        "ubuntu",
        "6.8.0-45-generic",
        purge=SimpleNamespace(ok=True, stdout=_APT_PURGE_OUTPUT),
    )

    assert len(purged) == 1
    assert result == (312_000_000, 1, 1)


def test_old_kernels_counts_nothing_when_the_purge_fails():
    purged, result = _purge_kernels(
        _UBUNTU_KERNEL_ROWS,
        "ubuntu",
        "6.8.0-45-generic",
        purge=SimpleNamespace(ok=False, stdout=""),
    )

    assert len(purged) == 1
    assert result == (0, 0, 0)


def test_apt_freed_bytes_reads_apts_sentence_and_nothing_else():
    from src.clean import system as module

    # Decimal, because apt's SizeToStr divides by 1000.
    assert module._apt_freed_bytes("After this operation, 312 MB disk space will be freed.") == (
        312_000_000
    )
    assert module._apt_freed_bytes("After this operation, 1.2 GB disk space will be freed.") == (
        1_200_000_000
    )
    # Under a kilobyte apt leaves two spaces before the B ("%.0f %c" + "B").
    assert module._apt_freed_bytes("After this operation, 512  B disk space will be freed.") == 512
    # An install is not a free: this wording must not be counted.
    assert module._apt_freed_bytes("After this operation, 12.3 MB of additional disk space") == 0
    # The line parse_size_from_text() would have read as 2 TB.
    assert module._apt_freed_bytes("0 upgraded, 0 newly installed, 2 to remove") == 0
    assert module._apt_freed_bytes("") == 0


def test_old_kernels_dnf_path():
    # The branch follows os-release, not whichever tools happen to be installed:
    # with dpkg present for `alien` -- as it is here -- a Fedora box used to take
    # the deb branch, find no linux-image-* rows and never ask dnf about kernels.
    rpm = "5.10.1-1\n5.10.2-1\n6.1.0-1\n"
    with (
        patch("src.clean.system.get_os_id", return_value="fedora"),
        patch("src.clean.system.platform.release", return_value="6.1.0-1"),
        patch("shutil.which", side_effect=lambda n: "/usr/bin/" + n),
        patch("src.clean.system.run_command", return_value=SimpleNamespace(ok=True, stdout=rpm)),
    ):
        assert clean_old_kernels(dry_run=True) == (0, 0, 1)
        assert clean_old_kernels() == (0, 1, 1)


def test_opensuse_cleans_its_package_cache_but_has_no_orphan_sweep():
    """openSUSE used to fall through every branch: no zypper row existed at all.

    Orphans stay out on purpose -- `zypper remove --clean-deps` needs the list of
    packages to remove, and zypper has no unprivileged equivalent of
    `apt-get autoremove` or `pacman -Qtdq` to produce one.
    """
    with (
        patch("src.clean.system.get_os_id", return_value="opensuse-leap"),
        patch("shutil.which", side_effect=lambda n: "/usr/bin/zypper" if n == "zypper" else None),
        patch("src.clean.system._get_package_manager_cache_paths", return_value=[]),
        patch("src.clean.system._measure_package_cache_size", side_effect=[500, 200]),
        patch(
            "src.clean.system.run_command", return_value=SimpleNamespace(ok=True, stdout="")
        ) as run,
    ):
        assert clean_package_manager() == (300, 1, 1)
        run.assert_called_once_with(
            ["zypper", "--non-interactive", "clean"],
            use_sudo=True,
            capture=True,
            env=C_LOCALE_ENV,
        )
        assert clean_orphaned_packages() == (0, 0, 0)


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
