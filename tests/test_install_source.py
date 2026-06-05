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
