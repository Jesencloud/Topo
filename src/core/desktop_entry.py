import shlex
from pathlib import Path


def parse_desktop_entry(path: str | Path) -> dict[str, str]:
    """Parse key/value pairs from a desktop-entry style file.

    The parser intentionally keeps this lightweight: it ignores comments and
    group headers, preserves localized keys such as ``Name[zh_CN]``, and returns
    the last value seen for duplicate keys.
    """
    entry_path = Path(path).expanduser()
    fields: dict[str, str] = {}
    try:
        for raw_line in entry_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith(("#", "[")) or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key:
                fields[key] = value.strip()
    except OSError:
        return {}
    return fields


def get_desktop_exec_command(path: str | Path) -> str:
    """Return the executable command from a desktop ``Exec=`` field.

    Malformed quoted commands return an empty string so callers can keep the
    entry instead of deleting something ambiguous.
    """
    exec_value = parse_desktop_entry(path).get("Exec", "")
    if not exec_value:
        return ""
    try:
        parts = shlex.split(exec_value)
    except ValueError:
        return ""
    for part in parts:
        if part.startswith("%"):
            continue
        return part
    return ""


def get_desktop_exec_names(path: str | Path) -> set[str]:
    """Return executable basenames from a desktop ``Exec=`` field."""
    command = get_desktop_exec_command(path)
    if not command:
        return set()
    name = Path(command).name.strip()
    return {name} if name and not name.startswith("%") else set()


def get_desktop_name(path: str | Path, locale: str = "zh_CN") -> str:
    fields = parse_desktop_entry(path)
    localized_key = f"Name[{locale}]" if locale else ""
    if localized_key and fields.get(localized_key):
        return fields[localized_key]
    return fields.get("Name", "")


def get_desktop_icon(path: str | Path) -> str:
    return parse_desktop_entry(path).get("Icon", "")
