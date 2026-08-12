import re

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
