from pathlib import Path

INSTALL_SOURCE_MARKER = ".topo-install-source"
SCRIPT_INSTALL = "script"
PACKAGE_INSTALL = "package"


def get_install_root() -> Path:
    """Return the Topo application root for both script and package installs."""
    return Path(__file__).parent.parent.parent


def get_install_source() -> str:
    """Return how this Topo copy was installed.

    Older script installs do not have the marker, so they are treated as script
    installs for backward compatibility.
    """
    marker = get_install_root() / INSTALL_SOURCE_MARKER
    try:
        value = marker.read_text().strip().lower()
    except OSError:
        return SCRIPT_INSTALL
    if value == PACKAGE_INSTALL:
        return PACKAGE_INSTALL
    return SCRIPT_INSTALL
