"""The distro matrix: one answer per machine, and two lookups that differ on purpose."""

from unittest.mock import patch

from src.core.install_source import (
    get_package_asset_name,
    get_package_remove_argv,
    get_package_upgrade_argv,
)
from src.core.package_manager import (
    APT,
    DNF,
    PACKAGE_MANAGERS,
    PACKAGE_QUERY_TOOLS,
    PACMAN,
    ZYPPER,
    detect_package_manager,
    find_package_manager,
    get_rpm_family_manager,
    resolve_admin_tool,
)


def _no_families(_family):
    return False


def test_every_row_is_distinct_and_self_consistent():
    assert len({m.key for m in PACKAGE_MANAGERS}) == len(PACKAGE_MANAGERS)
    assert len({m.label for m in PACKAGE_MANAGERS}) == len(PACKAGE_MANAGERS)
    # No id may name two managers, or "which manager owns this machine" would
    # depend on the order of the tuple.
    all_ids = [os_id for m in PACKAGE_MANAGERS for os_id in m.os_ids]
    assert len(all_ids) == len(set(all_ids))
    for manager in PACKAGE_MANAGERS:
        assert manager.admin_tools
        assert manager.package_format in (None, "deb", "rpm")
        # A manager topo can remove its own package with must also be able to say
        # what to remove; one without an asset simply declines to upgrade.
        assert manager.topo_remove_args
    # rpm answers for both DNF and Zypper, and the scan cache must ask once.
    assert PACKAGE_QUERY_TOOLS == ("dpkg-query", "rpm", "pacman")


def test_exact_ids_answer_for_every_family():
    assert find_package_manager("ubuntu") is APT
    assert find_package_manager("kali") is APT
    assert find_package_manager("fedora") is DNF
    assert find_package_manager("almalinux") is DNF
    assert find_package_manager("opensuse-tumbleweed") is ZYPPER
    assert find_package_manager("arch") is PACMAN
    # Case is not the caller's problem: os-release is lowercased on read, but
    # callers pass literals.
    assert find_package_manager("Ubuntu") is APT


def test_find_package_manager_never_guesses():
    """The strict lookup decides what gets downloaded, so it must fail safe.

    A Fedora box with the dpkg tools installed for `alien` must not be handed a
    .deb, and a distro nobody listed must come out as None rather than as the
    first manager whose binaries happen to exist.
    """
    with (
        patch("src.core.package_manager.is_os_family", side_effect=lambda _f: True),
        patch("shutil.which", side_effect=lambda tool: f"/usr/bin/{tool}"),
    ):
        assert find_package_manager("void") is None
        assert find_package_manager("unknown") is None


def test_detect_package_manager_falls_back_to_the_id_like_family():
    with (
        patch("src.core.package_manager.is_os_family", side_effect=lambda f: f == "debian"),
        patch("shutil.which", return_value=None),
    ):
        # raspbian is in nobody's id list; ID_LIKE=debian is how it says apt.
        assert detect_package_manager("raspbian") is APT


def test_detect_package_manager_probes_path_for_unlisted_distros():
    with (
        patch("src.core.package_manager.is_os_family", side_effect=_no_families),
        patch(
            "shutil.which", side_effect=lambda tool: "/usr/bin/pacman" if tool == "pacman" else None
        ),
    ):
        assert detect_package_manager("artix") is PACMAN
        # "unknown" is what get_os_id() reports with no readable /etc/os-release.
        # A machine that will not identify itself gets no guess from whichever
        # binaries exist -- and it is the one id whose answer cannot depend on the
        # machine the tests run on.
        assert detect_package_manager("unknown") is None


def test_get_rpm_family_manager_asks_os_release_not_path():
    with patch("src.core.package_manager.is_os_family", side_effect=lambda f: f == "suse"):
        assert get_rpm_family_manager() is ZYPPER
    with patch("src.core.package_manager.is_os_family", side_effect=_no_families):
        assert get_rpm_family_manager() is DNF


def test_resolve_admin_tool_prefers_the_newest_generation():
    with patch("shutil.which", side_effect=lambda tool: f"/usr/bin/{tool}"):
        assert resolve_admin_tool(DNF) == "dnf5"
    with patch("shutil.which", side_effect=lambda tool: "/usr/bin/dnf" if tool == "dnf" else None):
        assert resolve_admin_tool(DNF) == "dnf"
    # Nothing installed still names a command, so the caller can decide by
    # which()-ing the answer instead of re-deriving the list.
    with patch("shutil.which", return_value=None):
        assert resolve_admin_tool(DNF) == "dnf"
        assert resolve_admin_tool(APT) == "apt-get"


def test_the_release_assets_follow_the_matrix_package_format(monkeypatch):
    monkeypatch.setattr("src.core.install_source.os.geteuid", lambda: 1000)
    assert get_package_asset_name("v1.1.2", "ubuntu", "x86_64") == "topo_1.1.2-1_amd64.deb"
    assert get_package_asset_name("1.1.2", "opensuse-leap", "aarch64") == "topo-1.1.2-1.aarch64.rpm"
    # Arch is in the matrix so that removal works, but packaging builds only a
    # .deb and an .rpm -- so `topo update` still has nothing to download.
    assert PACMAN.package_format is None
    assert get_package_asset_name("1.1.2", "arch", "x86_64") is None
    assert get_package_upgrade_argv("/tmp/topo.pkg", "arch") is None
    assert get_package_remove_argv("arch") == ["sudo", "pacman", "-Rns", "--noconfirm", "topo"]


def test_the_update_transcript_keeps_the_unversioned_command_names(monkeypatch):
    """CI greps this output, and dnf5 is not the name every rpm release has."""
    monkeypatch.setattr("src.core.install_source.os.geteuid", lambda: 1000)
    assert get_package_upgrade_argv("/tmp/topo.rpm", "fedora") == [
        "sudo",
        "dnf",
        "upgrade",
        "-y",
        "/tmp/topo.rpm",
    ]
    assert get_package_remove_argv("fedora") == ["sudo", "dnf", "remove", "-y", "topo"]
    assert get_package_remove_argv("ubuntu") == ["sudo", "apt", "remove", "-y", "topo"]
