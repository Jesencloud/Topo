import re

# C0 / DEL / C1 (ANSI-CSI injection), Unicode BiDi overrides & isolates (Trojan
# Source), and the line/paragraph separators that str.splitlines() breaks on.
_UNSAFE_DISPLAY_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def sanitize_for_display(text: str) -> str:
    """Sanitizes control characters (including TAB/LF) in untrusted filenames before UI/logging output."""
    return _UNSAFE_DISPLAY_RE.sub("\ufffd", text)
