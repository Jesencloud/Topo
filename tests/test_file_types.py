"""Extension-to-icon mapping for the Analyze rows."""

from collections import Counter
from pathlib import Path

import pytest

from src.core.file_types import (
    _CATEGORIES,
    DEFAULT_FILE_ICON,
    DIRECTORY_ICON,
    icon_for_entry,
)


def test_no_suffix_is_claimed_by_two_categories():
    """The lookup is built by comprehension, so a repeat would silently win.

    Two categories listing the same suffix is not a crash, it is a table whose
    later half quietly overrides the earlier one -- exactly the sort of thing
    that survives review. .ts is a real collision (TypeScript vs MPEG transport
    stream) and is resolved by listing it once, under code.
    """
    suffixes = [suffix for _icon, group in _CATEGORIES for suffix in group]
    assert [s for s, n in Counter(suffixes).items() if n > 1] == []


def test_suffixes_are_written_without_a_dot_and_lowercased():
    """How the table is written is what makes the lookup keys correct."""
    for _icon, group in _CATEGORIES:
        for suffix in group:
            assert not suffix.startswith("."), suffix
            assert suffix == suffix.lower(), suffix


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("song.mp3", "🎶"),
        ("album.flac", "🎶"),
        ("clip.mp4", "📹"),
        ("movie.mkv", "📹"),
        ("photo.jpg", "🖼️"),
        ("icon.svg", "🖼️"),
        ("bundle.zip", "📦"),
        ("app.AppImage", "📦"),
        ("pkg.deb", "📦"),
        ("ubuntu-24.04.iso", "💿"),
        ("machine.qcow2", "💿"),
        ("disk.img", "💿"),
        ("notes.md", "📝"),
        ("report.pdf", "📝"),
        ("main.rs", "🧩"),
        ("script.sh", "🧩"),
    ],
)
def test_known_extensions_get_their_category(name, expected):
    assert icon_for_entry(name) == expected


@pytest.mark.parametrize(
    "name",
    ["Makefile", ".bashrc", "core.dump", "archive.", "no_extension", "data.unknownext"],
)
def test_unrecognised_and_suffixless_names_fall_back(name):
    assert icon_for_entry(name) == DEFAULT_FILE_ICON


def test_only_the_last_suffix_is_read():
    """A .tar.gz is caught by gz, which is why each compressor is listed."""
    assert icon_for_entry("backup.tar.gz") == "📦"
    assert icon_for_entry("backup.tar.zst") == "📦"
    assert icon_for_entry("backup.tar") == "📦"


def test_matching_ignores_case():
    assert icon_for_entry("HOLIDAY.MP4") == "📹"
    assert icon_for_entry("Archive.TAR.GZ") == "📦"


def test_a_directory_outranks_its_extension():
    """A directory named Backup.zip is a directory, not an archive."""
    assert icon_for_entry("Backup.zip", is_dir=True) == DIRECTORY_ICON
    assert icon_for_entry("node_modules", is_dir=True) == DIRECTORY_ICON
    assert icon_for_entry("Backup.zip", is_dir=False) == "📦"


def test_accepts_a_path_as_well_as_a_name():
    assert icon_for_entry(Path("/home/u/Videos/holiday.mkv")) == "📹"


def test_choosing_an_icon_touches_no_filesystem(tmp_path):
    """is_dir is passed in, never probed: the caller already knows it.

    A stat here would be one syscall per row on a screen that renders up to
    FAST_EXPLORE_ENTRY_LIMIT rows a frame.
    """
    missing = tmp_path / "does-not-exist.mp3"
    assert icon_for_entry(missing) == "🎶"
    assert not missing.exists()
