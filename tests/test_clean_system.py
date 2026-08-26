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

# `apt-get autoremove` as debian:stable-slim really narrates it. The Remv lines
# are the simulation's: a real run prints its per-package progress as "Removing
# tree (2.2.1-1) ..." instead, which is why the count has to come from the
# --dry-run pass and the freed total from the run itself.
_APT_AUTOREMOVE_DRY_RUN = """\
Reading package lists...
Building dependency tree...
Reading state information...
The following packages will be REMOVED:
  libgdbm6t64 tree
0 upgraded, 0 newly installed, 2 to remove and 2 not upgraded.
Remv tree [2.2.1-1]
Remv libgdbm6t64 [1.24-2]
"""
_APT_AUTOREMOVE_OUTPUT = """\
Reading package lists...
Building dependency tree...
Reading state information...
The following packages will be REMOVED:
  libgdbm6t64 tree
0 upgraded, 0 newly installed, 2 to remove and 2 not upgraded.
After this operation, 132 kB disk space will be freed.
Removing tree (2.2.1-1) ...
Removing libgdbm6t64:amd64 (1.24-2) ...
"""
_APT_NOTHING_TO_REMOVE = """\
Reading package lists...
Building dependency tree...
Reading state information...
0 upgraded, 0 newly installed, 0 to remove and 2 not upgraded.
"""
# `dnf autoremove -y` on dnf5, transcribed from `dnf --assumeno remove tree` on
# Fedora 44. The size column of the first table row is the trap: parse_size_from_text
# over this whole transcript answers 120.0 KiB whatever the transaction total is, and
# on a two-package removal it answers the size of one of them.
_DNF5_AUTOREMOVE_OUTPUT = """\
Package Arch   Version        Repository                            Size
Removing:
 tree   x86_64 0:2.2.1-4.fc44 19278be6a81040f5b6cbc7bacea5148e 120.0 KiB
 gdbm   x86_64 1:1.23-9.fc44  19278be6a81040f5b6cbc7bacea5148e 456.0 KiB

Transaction Summary:
 Removing:           2 packages

After this operation, 576 KiB will be freed (install 0 B, remove 576 KiB).
Running transaction
Complete!
"""
# dnf4, as RHEL 9 and Leap still run it: no colon after the heading, "Remove" in
# place of "Removing:", "Freed space:" in place of the sentence.
_DNF4_AUTOREMOVE_OUTPUT = """\
Dependencies resolved.
================================================================================
 Package        Arch    Version          Repository       Size
================================================================================
Removing:
 tree           x86_64  2.2.1-4.fc44     @System         120 k

Transaction Summary
================================================================================
Remove  1 Package

Freed space: 120 k
Complete!
"""


def test_package_cache_paths_cover_supported_managers(monkeypatch):
    from src.clean import system as module

    monkeypatch.setattr(module.Path, "exists", lambda self: True)
    # One family per row, taken from the table Analyze reads: apt's own
    # `partial/` subdirectory is not listed, because get_size_fast() recurses into
    # it and naming it separately counted its bytes twice. The two binary indexes
    # are listed -- they sit beside archives/ rather than inside it, and the same
    # `apt-get clean` unlinks them (D4).
    assert [str(p) for p in module._get_package_manager_cache_paths("apt")] == [
        "/var/cache/apt/archives",
        "/var/cache/apt/pkgcache.bin",
        "/var/cache/apt/srcpkgcache.bin",
    ]
    assert module._get_package_manager_cache_paths("unknown") == []


def test_apt_cache_measurement_counts_the_binary_indexes(tmp_path, monkeypatch):
    """A plain file among the cache paths is measured, and an absent one skipped.

    On debian:stable-slim archives/ held 8.2 MiB and the two indexes 84.9 MiB, so
    a measurement that skipped them reported a tenth of what `apt-get clean` went
    on to free (D4).
    """
    from src.clean import system as module
    from src.core.heavy_cache import CachePathDef

    archives = tmp_path / "archives"
    archives.mkdir()
    (archives / "some.deb").write_bytes(b"d" * 1000)
    index = tmp_path / "pkgcache.bin"
    index.write_bytes(b"i" * 9000)

    monkeypatch.setattr(
        module,
        "PACKAGE_MANAGER_CACHE_DEFS",
        (
            CachePathDef(
                key="apt",
                label="Apt Cache",
                path=str(archives),
                extra_paths=(str(index), str(tmp_path / "srcpkgcache.bin")),
            ),
        ),
    )

    paths = module._get_package_manager_cache_paths("apt")

    # The index that is not there is dropped, the way a missing fallback is.
    assert [str(p) for p in paths] == [str(archives), str(index)]
    # get_size_fast() stats a file instead of scanning it as a directory, so the
    # index contributes its own bytes rather than nothing.
    assert module._measure_package_cache_size(paths) == 10000


@patch("shutil.which", return_value=None)
def test_system_cleaners_no_tools_return_zero(_mock_which):
    assert clean_snaps() == (0, 0, 0)
    assert clean_journal() == (0, 0, 0)


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_orphaned_packages_fedora(mock_get_os_id, mock_run, mock_which):
    """dnf's own two numbers, from its transaction summary (D14).

    What this used to accept: parse_size_from_text() over the whole transcript and
    `stdout.count("\\n") // 2`. Against the fixture below the first answers 120 KiB --
    the size column of the table's first row -- for a transaction that freed 576, and
    the second answers 6 packages for a transaction that removed 2.
    """
    mock_get_os_id.return_value = "fedora"
    mock_which.side_effect = lambda x: "/usr/bin/dnf" if x == "dnf" else None
    mock_run.return_value = MagicMock(ok=True, stdout=_DNF5_AUTOREMOVE_OUTPUT)

    with patch("builtins.print") as mock_print:
        assert clean_orphaned_packages(dry_run=False) == (576 * 1024, 2, 1)

    assert "Removed 2 orphaned DNF package(s) (576.0 KiB)" in mock_print.call_args[0][0]
    mock_run.assert_called_with(
        ["dnf", "autoremove", "-y"], use_sudo=True, capture=True, env=C_LOCALE_ENV
    )

    # dnf4 is still what RHEL 9 and Leap run, and it words all three of the lines
    # being read here differently.
    mock_run.return_value = MagicMock(ok=True, stdout=_DNF4_AUTOREMOVE_OUTPUT)
    assert clean_orphaned_packages(dry_run=False) == (120 * 1024, 1, 1)

    # The preview stays wordless: `dnf autoremove` needs the transaction lock even to
    # resolve, so there is no unprivileged count to print the way apt has one.
    mock_run.reset_mock()
    assert clean_orphaned_packages(dry_run=True) == (0, 0, 1)
    mock_run.assert_not_called()


@patch("shutil.which", side_effect=lambda x: "/usr/bin/dnf" if x == "dnf" else None)
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id", return_value="fedora")
def test_a_dnf_transaction_that_says_nothing_is_reported_as_nothing(
    _mock_get_os_id, mock_run, _mock_which
):
    """0 rather than a guess, and no line claiming a removal that did not happen.

    Two ways to get here and both are answered the same: dnf removed nothing, or it
    reported the transaction in a dialect these patterns do not read. The old code
    printed "Removed orphaned DNF packages" for both -- with a size taken from
    wherever in the output a number happened to sit -- and counted "Nothing to do."
    as half a package.
    """
    for output in ("Nothing to do.\n", "Removed 500 MB\nPackage1\nPackage2\n"):
        mock_run.return_value = MagicMock(ok=True, stdout=output)
        with patch("builtins.print") as mock_print:
            assert clean_orphaned_packages(dry_run=False) == (0, 0, 0)
        mock_print.assert_not_called()


@patch("shutil.which")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id")
def test_clean_orphaned_packages_ubuntu(mock_get_os_id, mock_run, mock_which):
    mock_get_os_id.return_value = "ubuntu"
    mock_which.return_value = "/usr/bin/apt-get"
    mock_run.side_effect = lambda args, **kwargs: MagicMock(
        ok=True,
        stdout=_APT_AUTOREMOVE_DRY_RUN if "--dry-run" in args else _APT_AUTOREMOVE_OUTPUT,
    )

    s, i, c = clean_orphaned_packages(dry_run=False)

    # apt's own two numbers: one Purg line per package, and the freed total in the
    # decimal kB apt-pkg prints. 132 kB is 132000 bytes, not 132 * 1024 -- which is
    # what parse_size_from_text over the whole transcript used to make of it.
    assert (s, i, c) == (132000, 2, 1)
    # apt-get, unlike dnf, runs maintainer scripts that may ask debconf a question
    # with nobody able to see the prompt.
    mock_run.assert_called_with(
        ["apt-get", "autoremove", "-y"],
        use_sudo=True,
        capture=True,
        env=APT_NONINTERACTIVE_ENV,
    )


@patch("shutil.which", return_value="/usr/bin/apt-get")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id", return_value="ubuntu")
def test_clean_orphaned_packages_dry_run_counts_what_apt_would_remove(
    _mock_get_os_id, mock_run, _mock_which
):
    """The preview says how many, and asks nothing of root to find out.

    It used to print "Orphaned APT packages would be autoremoved" whatever the
    machine's state: no count, and no idea whether there was anything to remove.
    """
    mock_run.return_value = MagicMock(ok=True, stdout=_APT_AUTOREMOVE_DRY_RUN)

    with patch("builtins.print") as mock_print:
        assert clean_orphaned_packages(dry_run=True) == (0, 0, 1)

    assert "2 orphaned APT package(s) would be removed" in mock_print.call_args[0][0]
    # One command, unprivileged: --dry-run is what makes that possible, and it
    # narrates the same transaction `-y` would run.
    mock_run.assert_called_once_with(
        ["apt-get", "autoremove", "--dry-run"], capture=True, env=APT_NONINTERACTIVE_ENV
    )


@patch("shutil.which", return_value="/usr/bin/apt-get")
@patch("src.clean.system.run_command")
@patch("src.clean.system.get_os_id", return_value="ubuntu")
def test_clean_orphaned_packages_reports_nothing_when_there_are_no_orphans(
    _mock_get_os_id, mock_run, _mock_which
):
    """A clean machine is not a cleaned item, and a failed query is not either."""
    mock_run.return_value = MagicMock(ok=True, stdout=_APT_NOTHING_TO_REMOVE)
    assert clean_orphaned_packages(dry_run=True) == (0, 0, 0)
    assert clean_orphaned_packages(dry_run=False) == (0, 0, 0)

    mock_run.return_value = MagicMock(ok=False, stdout="")
    assert clean_orphaned_packages(dry_run=False) == (0, 0, 0)

    # Whatever the answer, nothing was ever removed: only the query ran.
    assert [call.args[0] for call in mock_run.call_args_list] == [
        ["apt-get", "autoremove", "--dry-run"]
    ] * 3


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


# What `dpkg-query -W -f='${db:Status-Abbrev}\t${Package}\n' 'linux-image-*'`
# prints: the status triple (want, state, error), a tab, the name -- no header and
# no fixed-width columns. The row order is dpkg's own -- alphabetical, which puts
# "-100-" ahead of "-31-" and the metapackage last -- because the code under test
# must not read it as a version order.
_UBUNTU_KERNEL_ROWS = """\
ii \tlinux-image-6.8.0-100-generic
ii \tlinux-image-6.8.0-31-generic
ii \tlinux-image-6.8.0-45-generic
ii \tlinux-image-generic
"""
_DEBIAN_KERNEL_ROWS = """\
ii \tlinux-image-5.10.0-26-amd64
ii \tlinux-image-5.10.0-28-amd64
ii \tlinux-image-6.1.0-18-amd64
ii \tlinux-image-amd64
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


# A removal that worked and said nothing about sizes -- shared by both branches.
_REMOVAL_OK = SimpleNamespace(ok=True, stdout="")


def _purge_kernels(rows, os_id, release, purge=_REMOVAL_OK, dry_run=False):
    """clean_old_kernels() over a `dpkg-query -W` listing; returns (purged, result)."""
    purged = []

    def fake_run_command(args, **kwargs):
        if args[:2] == ["dpkg-query", "-W"]:
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


def test_old_kernels_asks_the_matrix_query_tool_and_respects_a_hold():
    """The kernel listing comes from dpkg-query, the matrix's query_tool (D10).

    `dpkg -l` was a second hand-written answer to "what reads the installed
    database" -- doctor probes dpkg-query, uninstall's scanner reads dpkg-query --
    and it made the code parse a fixed-width table for two fields it can simply
    ask for. The status pair still has to be read, though: `apt-mark hold` shows up
    as "hi", and the old `startswith("ii")` kept held kernels out of the purge list
    for free. Losing that would have made `topo clean` purge a package the user
    explicitly pinned.
    """
    queries = []
    purged = []

    def fake_run_command(args, **kwargs):
        if args[0] == "dpkg-query":
            queries.append(args)
            return SimpleNamespace(
                ok=True,
                stdout=(
                    "ii \tlinux-image-6.8.0-100-generic\n"
                    "hi \tlinux-image-6.8.0-31-generic\n"  # held by the user
                    "rc \tlinux-image-6.8.0-40-generic\n"  # removed, config files left
                    "iU \tlinux-image-6.8.0-41-generic\n"  # unpacked, not configured
                    "ii \tlinux-image-6.8.0-45-generic\n"  # running
                    "ii \tlinux-image-6.8.0-52-generic\n"
                    "\n"  # dpkg-query prints nothing for an empty pattern match
                ),
            )
        purged.append(args)
        return _REMOVAL_OK

    with (
        patch("src.clean.system.get_os_id", return_value="ubuntu"),
        patch("src.clean.system.platform.release", return_value="6.8.0-45-generic"),
        patch("shutil.which", side_effect=lambda n: "/usr/bin/" + n),
        patch("src.clean.system.run_command", side_effect=fake_run_command),
    ):
        result = clean_old_kernels()

    assert queries == [
        [
            "dpkg-query",
            "-W",
            "-f=${db:Status-Abbrev}\t${Package}\n",
            "linux-image-*",
        ]
    ]
    # Of the three "ii" rows, the running kernel stays and so does the newest of
    # the rest (-100-, which sorts above -52- by version and below it
    # alphabetically); only -52- goes. The held, the half-removed and the unpacked
    # rows never entered the candidate list at all.
    assert purged == [["apt-get", "purge", "-y", "linux-image-6.8.0-52-generic"]]
    assert result == (0, 1, 1)


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


# Real `dnf repoquery --installonly --latest-limit=-2` output from a box with
# three kernels installed: every subpackage of the oldest one, epoch and all.
# dnf5 lists them sorted by name, and one kernel is five or six rows -- which is
# why the old per-row `removable[:-1]` left one of them behind.
_FEDORA_STALE_ROWS = """\
kernel-0:7.1.8-200.fc44.x86_64
kernel-core-0:7.1.8-200.fc44.x86_64
kernel-modules-0:7.1.8-200.fc44.x86_64
kernel-modules-core-0:7.1.8-200.fc44.x86_64
kernel-modules-extra-0:7.1.8-200.fc44.x86_64
"""
# The tail of what `dnf remove -y` prints. dnf5 counts in MiB, unlike apt's MB.
_DNF5_REMOVE_OUTPUT = """\
Transaction Summary:
 Removing:           5 packages

After this operation, 312 MiB will be freed (install 0 B, remove 312 MiB).
Running transaction
"""


def _remove_kernels(rows, release, remove=_REMOVAL_OK, dry_run=False):
    """clean_old_kernels() over a `dnf repoquery` listing; returns (calls, result).

    `calls` holds (argv, env) for every command, the query included: which
    arguments the query carries is itself the D13 regression.
    """
    calls = []

    def fake_run_command(args, **kwargs):
        calls.append((args, kwargs.get("env")))
        if "repoquery" in args:
            return SimpleNamespace(ok=True, stdout=rows)
        return remove

    with (
        patch("src.clean.system.get_os_id", return_value="fedora"),
        patch("src.clean.system.platform.release", return_value=release),
        patch("shutil.which", side_effect=lambda n: "/usr/bin/" + n),
        patch("src.clean.system.run_command", side_effect=fake_run_command),
    ):
        return calls, clean_old_kernels(dry_run=dry_run)


def test_old_kernels_asks_dnf_a_question_dnf5_accepts():
    """dnf5 refuses `--installonly --installed` as a mutually exclusive pair, so
    the query failed and the branch returned empty-handed on every Fedora 41+.

    The branch is also chosen by os-release, not by whichever tools happen to be
    installed: with dpkg present for `alien` -- as it is on the box these fixtures
    came from -- a Fedora box used to take the deb branch, find no linux-image-*
    rows and never ask dnf about kernels at all.
    """
    calls, result = _remove_kernels(_FEDORA_STALE_ROWS, "7.1.10-200.fc44.x86_64", dry_run=True)

    assert calls == [
        (["dnf5", "repoquery", "--installonly", "--latest-limit=-2"], C_LOCALE_ENV),
    ]
    # One kernel, not the five rows it arrived as.
    assert result == (0, 0, 1)


def test_old_kernels_removes_every_subpackage_of_a_stale_kernel_together():
    """`removable[:-1]` counted rows, so of the five packages that make up this
    one kernel it removed four and called the leftover the fallback kernel -- a
    kernel-modules with no image behind it, which dnf's dependency resolution
    then took away as well.
    """
    calls, result = _remove_kernels(
        _FEDORA_STALE_ROWS,
        "7.1.10-200.fc44.x86_64",
        remove=SimpleNamespace(ok=True, stdout=_DNF5_REMOVE_OUTPUT),
    )

    # C locale on the removal too, not just the query: the sentence it is read
    # for is one dnf5 translates.
    assert calls[1:] == [
        (["dnf5", "remove", "-y", *_FEDORA_STALE_ROWS.split()], C_LOCALE_ENV),
    ]
    # 312 MiB, because dnf reports in 1024s where apt reports in 1000s.
    assert result == (312 * 1024**2, 1, 1)


def test_old_kernels_leaves_the_running_kernel_out_of_the_transaction():
    # A box booted into something older than its two newest kernels: repoquery's
    # "all but the two newest" then names the running kernel, and dnf would refuse
    # the whole transaction over it (protect_running_kernel).
    calls, result = _remove_kernels(_FEDORA_STALE_ROWS, "7.1.8-200.fc44.x86_64")

    assert [argv[1] for argv, _env in calls] == ["repoquery"]
    assert result == (0, 0, 0)


def test_old_kernels_ignores_dnf_output_that_is_not_a_package():
    rows = _FEDORA_STALE_ROWS + "Last metadata expiration check: 0:03:21 ago.\n"

    calls, result = _remove_kernels(rows, "7.1.10-200.fc44.x86_64")

    # Whatever cannot be read as a package stays out of the argv, rather than
    # being handed to dnf to reject the batch over.
    assert [argv for argv, _env in calls[1:]] == [
        ["dnf5", "remove", "-y", *_FEDORA_STALE_ROWS.split()]
    ]
    assert result == (0, 1, 1)


def test_old_kernels_counts_nothing_when_the_dnf_transaction_fails():
    calls, result = _remove_kernels(
        _FEDORA_STALE_ROWS,
        "7.1.10-200.fc44.x86_64",
        remove=SimpleNamespace(ok=False, stdout=""),
    )

    assert [argv[1] for argv, _env in calls] == ["repoquery", "remove"]
    assert result == (0, 0, 0)


def test_rpm_kernel_version_is_the_uname_form_whatever_the_subpackage():
    from src.clean import system as module

    # Every subpackage of one kernel has to yield one key, or they get split
    # across the keep/remove line.
    assert {module._rpm_kernel_version(nevra) for nevra in _FEDORA_STALE_ROWS.split()} == {
        "7.1.8-200.fc44.x86_64"
    }
    # rpm -q prints no epoch; aarch64's kernel-64k carries digits in its *name*.
    assert module._rpm_kernel_version("kernel-core-7.1.8-200.fc44.x86_64") == (
        "7.1.8-200.fc44.x86_64"
    )
    assert module._rpm_kernel_version("kernel-64k-core-0:6.11.3-200.fc41.aarch64") == (
        "6.11.3-200.fc41.aarch64"
    )
    assert module._rpm_kernel_version("Last") == ""


def test_dnf_freed_bytes_reads_either_dnf_generations_wording():
    from src.clean import system as module

    # dnf5 (Fedora 41+), then dnf4's transaction table. Both count in 1024s.
    assert module._dnf_freed_bytes(
        "After this operation, 4 MiB will be freed (install 0 B, remove 4 MiB)."
    ) == (4 * 1024**2)
    assert module._dnf_freed_bytes("Freed space: 312 M\n") == 312 * 1024**2
    # An install is not a free, and neither is the row count.
    assert module._dnf_freed_bytes("After this operation, 4 MiB will be used") == 0
    assert module._dnf_freed_bytes("Removing 5 packages") == 0
    assert module._dnf_freed_bytes("") == 0


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
