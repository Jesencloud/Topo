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
    assert install_source.get_package_manager_commands("update", os_id="ubuntu") == [
        "sudo apt upgrade topo"
    ]
    assert install_source.get_package_manager_commands("remove", os_id="ubuntu") == [
        "sudo apt remove topo"
    ]


def test_get_package_manager_commands_uses_dnf_for_fedora():
    assert install_source.get_package_manager_commands("update", os_id="fedora") == [
        "sudo dnf upgrade topo"
    ]
    assert install_source.get_package_manager_commands("remove", os_id="fedora") == [
        "sudo dnf remove topo"
    ]


def test_get_package_manager_commands_falls_back_to_common_managers():
    assert install_source.get_package_manager_commands("update", os_id="unknown") == [
        "sudo apt upgrade topo",
        "sudo dnf upgrade topo",
    ]
