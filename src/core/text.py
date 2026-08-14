import re
import unicodedata

# The single definition of "must never reach a terminal or a log line raw":
# C0 / DEL / C1 (ANSI-CSI injection), the line/paragraph separators that
# str.splitlines() breaks on, and the Unicode BiDi overrides & isolates behind
# Trojan Source (CVE-2021-42574).
#
# Kept as one source of truth because two consumers need the same *set* with
# different output: sanitize_for_display() replaces, while the audit log escapes
# (see file_ops._sanitize_audit_field). Two copies of the ranges would drift.
UNSAFE_DISPLAY_RANGES: tuple[tuple[int, int], ...] = (
    (0x00, 0x1F),
    (0x7F, 0x9F),
    (0x2028, 0x202E),
    (0x2066, 0x2069),
)

_UNSAFE_DISPLAY_RE = re.compile(
    "[" + "".join(f"\\u{low:04x}-\\u{high:04x}" for low, high in UNSAFE_DISPLAY_RANGES) + "]"
)


def is_unsafe_display_char(code: int) -> bool:
    """Return True when *code* is a codepoint that must be escaped or replaced."""
    return any(low <= code <= high for low, high in UNSAFE_DISPLAY_RANGES)


def sanitize_for_display(text: str) -> str:
    """Sanitizes control characters (including TAB/LF) in untrusted filenames before UI/logging output."""
    return _UNSAFE_DISPLAY_RE.sub("\ufffd", text)


# U+FE00..U+FE0F. VS16 (U+FE0F) in particular is what makes an emoji like
# U+1F5C2 two codepoints, and it carries no advance of its own -- the terminal
# applies it to the base glyph.
_VARIATION_SELECTORS = range(0xFE00, 0xFE10)


def char_width(char: str) -> int:
    """Terminal cells *char* occupies.

    Zero for combining marks, format characters and variation selectors; two for
    East-Asian Wide/Fullwidth; one otherwise. Counting variation selectors as
    zero is what makes the icon columns line up: U+23F1 and U+1F5C2 are a narrow
    base plus VS16, so they measure 1, while U+1F4CA and U+1F4C4 measure 2 --
    which is how terminals actually render them. Treating VS16 as a cell of its
    own inflated the first two to 2 and forced the hand-tuned spacing that
    status.py and the selectors used to carry.
    """
    if unicodedata.combining(char) or unicodedata.category(char) == "Cf":
        return 0
    if ord(char) in _VARIATION_SELECTORS:
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    """Total terminal cells *text* occupies. Assumes ANSI escapes are stripped."""
    return sum(char_width(char) for char in text)
