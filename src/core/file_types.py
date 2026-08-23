"""Row icons for the Analyze screens, picked from a file's extension.

Deliberately extension-based rather than content sniffing. One Analyze frame
lists up to FAST_EXPLORE_ENTRY_LIMIT rows, and reading each file's header would
add an I/O round trip per row to a path that already stats for size and age.
stdlib ``mimetypes`` buys nothing here either -- it is extension-based too, but
its table is assembled from the system's /etc/mime.types and therefore differs
between distributions, which would make the same directory render differently
on Fedora and Debian.

The categories stop at a handful because past that the icons stop being
scannable at a glance, and they lean towards what actually occupies a disk --
video, archives, images, disk images -- since finding that is what this screen
is for. The small-text categories earn their place by saying what a directory
holds rather than by being cleanup targets.
"""

from pathlib import Path

DIRECTORY_ICON = "📁"
DEFAULT_FILE_ICON = "📄"

# (icon, suffixes) rather than a flat map so the source reads as the category
# table it is. Suffixes are written without the leading dot.
_CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "🎶",
        (
            "mp3",
            "wav",
            "flac",
            "ogg",
            "oga",
            "opus",
            "m4a",
            "m4b",
            "aac",
            "wma",
            "aiff",
            "aif",
            "alac",
            "mid",
            "midi",
        ),
    ),
    (
        "📹",
        (
            "mp4",
            "mkv",
            "avi",
            "mov",
            "webm",
            "wmv",
            "flv",
            "m4v",
            "mpg",
            "mpeg",
            "vob",
            "ogv",
            "rmvb",
            "3gp",
        ),
    ),
    (
        "🖼️",
        (
            "png",
            "jpg",
            "jpeg",
            "gif",
            "webp",
            "svg",
            "bmp",
            "tiff",
            "tif",
            "ico",
            "heic",
            "heif",
            "avif",
            "raw",
            "cr2",
            "nef",
            "arw",
            "dng",
            "psd",
            "xcf",
        ),
    ),
    (
        # Whole-filesystem images, kept apart from plain archives: an .iso or a
        # .qcow2 is usually the single largest thing in a directory, and telling
        # one apart from a .zip at a glance is most of what this screen is for.
        "💿",
        ("iso", "img", "dmg", "qcow2", "vmdk", "vdi", "vhd"),
    ),
    (
        # Archives and installers: containers holding files rather than a
        # filesystem. Only the final suffix is examined, so a .tar.gz is caught
        # by gz and a .tar.zst by zst -- each compressor needs its own entry,
        # not "tar".
        "📦",
        (
            "zip",
            "tar",
            "gz",
            "tgz",
            "xz",
            "txz",
            "bz2",
            "tbz2",
            "7z",
            "rar",
            "zst",
            "lz4",
            "lzma",
            "deb",
            "rpm",
            "apk",
            "appimage",
            "snap",
            "flatpak",
            "pkg",
            "msi",
            "exe",
            "cab",
        ),
    ),
    (
        "📝",
        (
            "pdf",
            "doc",
            "docx",
            "xls",
            "xlsx",
            "ppt",
            "pptx",
            "txt",
            "md",
            "rst",
            "odt",
            "ods",
            "odp",
            "rtf",
            "epub",
            "mobi",
            "azw3",
            "djvu",
            "csv",
            "tsv",
        ),
    ),
    (
        # .ts is both TypeScript and an MPEG transport stream. TypeScript wins:
        # on a Linux box being scanned for space, node_modules is far likelier
        # than a DVR capture.
        "🧩",
        (
            "py",
            "rs",
            "js",
            "ts",
            "jsx",
            "tsx",
            "sh",
            "bash",
            "zsh",
            "fish",
            "c",
            "h",
            "cpp",
            "cc",
            "cxx",
            "hpp",
            "java",
            "go",
            "rb",
            "php",
            "lua",
            "pl",
            "pm",
            "kt",
            "swift",
            "cs",
            "scala",
            "clj",
            "ex",
            "exs",
            "vim",
            "el",
            "sql",
            "json",
            "toml",
            "yaml",
            "yml",
            "xml",
            "html",
            "htm",
            "css",
            "scss",
            "ipynb",
        ),
    ),
)

_ICON_BY_SUFFIX: dict[str, str] = {
    f".{suffix}": icon for icon, suffixes in _CATEGORIES for suffix in suffixes
}


def icon_for_entry(name: str | Path, *, is_dir: bool = False) -> str:
    """Pick the row icon for *name*.

    ``is_dir`` outranks the extension -- a directory called Backup.zip is still a
    directory -- and is passed in rather than probed here: the fast-explore path
    already knows it from its own scan and would otherwise pay a stat per row.

    Only the last suffix is considered, lowercased, so ARCHIVE.TAR.GZ resolves
    through ``.gz``. Anything unrecognised, and anything with no suffix at all
    (Makefile, .bashrc), falls back to the plain-file icon.
    """
    if is_dir:
        return DIRECTORY_ICON
    return _ICON_BY_SUFFIX.get(Path(name).suffix.lower(), DEFAULT_FILE_ICON)
