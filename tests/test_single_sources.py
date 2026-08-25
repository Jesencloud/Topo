"""The things topo must not have more than one answer for.

All of them used to be copied around: the VERSION file was read by three
components with three different fallbacks, ~/.config/topo was spelled out by
hand in six places next to the get_config_dir() that exists for it, and the XDG
state directory was derived three times -- twice by the command that deletes it.
"""

from pathlib import Path

from src.core import constants
from src.core.config import get_config_file
from src.core.constants import (
    DETECTED_APPS_FILE,
    TOPO_VERSION,
    UNKNOWN_VERSION,
    read_topo_version,
)
from src.core.file_ops import get_deletion_log_path
from src.core.install_source import get_install_root
from src.core.lock import LOCK_FILE_PATH
from src.core.paths import get_config_dir, get_state_dir
from src.core.whitelist import get_whitelist_file
from src.manage.update import _parse_version


def test_read_topo_version_strips_the_stored_value(tmp_path):
    version_file = tmp_path / "VERSION"
    version_file.write_text("  1.2.3\n")

    assert read_topo_version(version_file) == "1.2.3"


def test_read_topo_version_reports_anything_unusable_as_none(tmp_path):
    # One definition of "cannot be read", shared by the three callers that used
    # to invent their own: missing, empty and whitespace-only all mean the same.
    assert read_topo_version(tmp_path / "absent") is None
    empty = tmp_path / "VERSION"
    empty.write_text("")
    assert read_topo_version(empty) is None
    empty.write_text("\n \n")
    assert read_topo_version(empty) is None


def test_unknown_version_cannot_be_mistaken_for_a_version():
    # The updater compares TOPO_VERSION against the latest release tag, so the
    # fallback must fail to parse: it used to be 0.0.0, which made every remote
    # tag look newer and turned a lost VERSION file into an unasked reinstall.
    assert _parse_version(UNKNOWN_VERSION) is None


def test_topo_version_comes_from_the_version_file():
    assert (read_topo_version() or UNKNOWN_VERSION) == TOPO_VERSION
    assert constants.VERSION_FILE.name == "VERSION"


def test_doctor_reads_the_same_version_file_as_the_banner():
    # doctor reports on the tree at get_install_root(); constants resolves its
    # own __file__. Both have to name the same file, or `topo doctor` and
    # `topo --version` could still disagree -- which is the whole point of
    # having one reader.
    assert get_install_root() / "VERSION" == constants.VERSION_FILE


def test_every_topo_config_path_hangs_off_get_config_dir():
    config_dir = get_config_dir()

    assert config_dir / "topo.lock" == LOCK_FILE_PATH
    assert config_dir / "detected_apps.json" == DETECTED_APPS_FILE
    assert get_config_file() == config_dir / "config.json"
    assert get_whitelist_file() == config_dir / "whitelist.json"


def test_the_audit_log_and_topo_remove_agree_on_the_state_dir(monkeypatch):
    # `topo remove` deletes this directory and the deletion log creates it, from
    # what used to be three separate XDG_STATE_HOME derivations. If they drift,
    # removal silently leaves the history behind.
    monkeypatch.setenv("XDG_STATE_HOME", "/tmp/topo-state-probe")

    assert get_state_dir() == Path("/tmp/topo-state-probe/topo")
    assert get_deletion_log_path() == get_state_dir() / "deletions.log"

    monkeypatch.delenv("XDG_STATE_HOME")
    assert get_state_dir() == Path.home() / ".local/state/topo"
