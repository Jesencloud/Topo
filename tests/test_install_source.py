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


def test_get_package_manager_commands_uses_apt_for_ubuntu():
    assert install_source.get_package_manager_commands(
        "update", os_id="ubuntu", machine="x86_64"
    ) == ["sudo apt install ./topo_xxx_amd64.deb"]
    assert install_source.get_package_manager_commands("remove", os_id="ubuntu") == [
        "sudo apt remove topo"
    ]


def test_get_package_manager_commands_uses_dnf_for_fedora():
    assert install_source.get_package_manager_commands(
        "update", os_id="fedora", machine="x86_64"
    ) == ["sudo dnf upgrade ./topo-xxx-1.x86_64.rpm"]
    assert install_source.get_package_manager_commands("remove", os_id="fedora") == [
        "sudo dnf remove topo"
    ]


def test_get_package_manager_commands_uses_arm64_package_names():
    assert install_source.get_package_manager_commands(
        "update", os_id="ubuntu", machine="aarch64"
    ) == ["sudo apt install ./topo_xxx_arm64.deb"]
    assert install_source.get_package_manager_commands(
        "update", os_id="fedora", machine="aarch64"
    ) == ["sudo dnf upgrade ./topo-xxx-1.aarch64.rpm"]


def test_get_package_manager_commands_falls_back_to_common_managers():
    assert install_source.get_package_manager_commands(
        "update", os_id="unknown", machine="x86_64"
    ) == [
        "sudo apt install ./topo_xxx_amd64.deb",
        "sudo dnf upgrade ./topo-xxx-1.x86_64.rpm",
    ]


def test_get_package_asset_name_uses_distro_and_arch():
    assert (
        install_source.get_package_asset_name("v1.2.3", os_id="ubuntu", machine="x86_64")
        == "topo_1.2.3_amd64.deb"
    )
    assert (
        install_source.get_package_asset_name("v1.2.3", os_id="ubuntu", machine="aarch64")
        == "topo_1.2.3_arm64.deb"
    )
    assert (
        install_source.get_package_asset_name("v1.2.3", os_id="fedora", machine="x86_64")
        == "topo-1.2.3-1.x86_64.rpm"
    )
    assert (
        install_source.get_package_asset_name("v1.2.3", os_id="fedora", machine="aarch64")
        == "topo-1.2.3-1.aarch64.rpm"
    )
    assert install_source.get_package_asset_name("v1.2.3", os_id="unknown") is None


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
