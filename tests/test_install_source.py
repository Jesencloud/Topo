from src.core import install_source


def test_get_install_source_defaults_to_script(monkeypatch, tmp_path):
    monkeypatch.setattr(install_source, "get_install_root", lambda: tmp_path)

    assert install_source.get_install_source() == install_source.SCRIPT_INSTALL


def test_get_install_source_reads_package_marker(monkeypatch, tmp_path):
    monkeypatch.setattr(install_source, "get_install_root", lambda: tmp_path)
    (tmp_path / install_source.INSTALL_SOURCE_MARKER).write_text("package\n")

    assert install_source.get_install_source() == install_source.PACKAGE_INSTALL


def test_get_install_source_treats_unknown_marker_as_script(monkeypatch, tmp_path):
    monkeypatch.setattr(install_source, "get_install_root", lambda: tmp_path)
    (tmp_path / install_source.INSTALL_SOURCE_MARKER).write_text("unknown\n")

    assert install_source.get_install_source() == install_source.SCRIPT_INSTALL


def test_get_package_asset_name_uses_distro_and_arch():
    assert (
        install_source.get_package_asset_name("v1.2.3", os_id="ubuntu", machine="x86_64")
        == "topo_1.2.3-1_amd64.deb"
    )
    assert (
        install_source.get_package_asset_name("v1.2.3", os_id="ubuntu", machine="aarch64")
        == "topo_1.2.3-1_arm64.deb"
    )
    assert (
        install_source.get_package_asset_name("v1.2.3", os_id="fedora", machine="x86_64")
        == "topo-1.2.3-1.x86_64.rpm"
    )
    assert (
        install_source.get_package_asset_name("v1.2.3", os_id="fedora", machine="aarch64")
        == "topo-1.2.3-1.aarch64.rpm"
    )
    assert (
        install_source.get_package_asset_name("v1.2.3", os_id="opensuse-leap", machine="x86_64")
        == "topo-1.2.3-1.x86_64.rpm"
    )
    assert install_source.get_package_asset_name("v1.2.3", os_id="unknown") is None


def test_get_package_asset_name_names_no_file_for_an_arch_with_no_package():
    """An unlisted machine gets None, not the amd64 file (D8).

    topo publishes packages for two architectures. The default used to be amd64
    (x86_64 for the rpm), so on an armv7l or riscv64 Debian `topo update`
    downloaded topo_<v>-1_amd64.deb -- which exists, so the checksum verified --
    and only `apt install` said "wrong architecture", after the download.
    """
    for os_id in ("ubuntu", "fedora"):
        for machine in ("armv7l", "i686", "riscv64", "ppc64le"):
            assert install_source.get_package_asset_name("v1.2.3", os_id, machine) is None
    # The four names that do exist still answer, whichever spelling the machine
    # reports and whatever the case.
    assert (
        install_source.get_package_asset_name("v1.2.3", "debian", "AMD64")
        == "topo_1.2.3-1_amd64.deb"
    )
    assert (
        install_source.get_package_asset_name("v1.2.3", "debian", "arm64")
        == "topo_1.2.3-1_arm64.deb"
    )


def test_get_package_execution_argv_uses_sudo_for_non_root(monkeypatch, tmp_path):
    monkeypatch.setattr(install_source.os, "geteuid", lambda: 1000)
    package_path = tmp_path / "topo.deb"

    assert install_source.get_package_upgrade_argv(package_path, os_id="ubuntu") == [
        "sudo",
        "apt",
        "install",
        "-y",
        str(package_path),
    ]
    assert install_source.get_package_remove_argv(os_id="fedora") == [
        "sudo",
        "dnf",
        "remove",
        "-y",
        "topo",
    ]
    assert install_source.get_package_upgrade_argv(package_path, os_id="opensuse-leap") == [
        "sudo",
        "zypper",
        "--non-interactive",
        "install",
        "--allow-unsigned-rpm",
        str(package_path),
    ]


def test_get_package_execution_argv_omits_sudo_for_root(monkeypatch, tmp_path):
    monkeypatch.setattr(install_source.os, "geteuid", lambda: 0)
    package_path = tmp_path / "topo.rpm"

    assert install_source.get_package_upgrade_argv(package_path, os_id="fedora") == [
        "dnf",
        "upgrade",
        "-y",
        str(package_path),
    ]
    assert install_source.get_package_remove_argv(os_id="ubuntu") == [
        "apt",
        "remove",
        "-y",
        "topo",
    ]
    assert install_source.get_package_remove_argv(os_id="opensuse-leap") == [
        "zypper",
        "--non-interactive",
        "remove",
        "topo",
    ]
