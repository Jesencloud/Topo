import shlex
from pathlib import Path

# Generic launchers and interpreters that a .desktop Exec= may put in front of
# the real program. Their basenames are worthless as an app identity: a
# `python`/`sh`/`node` process anywhere on the machine would match, and turning
# one into a pkill pattern or a residue keyword risks killing an unrelated
# process or trashing an unrelated directory. Matched by basename so
# `/usr/bin/python3` is recognised too.
_LAUNCHER_NAMES: frozenset[str] = frozenset(
    {
        "env",
        "sh",
        "bash",
        "dash",
        "zsh",
        "python",
        "python2",
        "python3",
        "perl",
        "ruby",
        "node",
        "nodejs",
        "flatpak",
        "snap",
        "sudo",
        "pkexec",
        "exec",
    }
)


def parse_desktop_entry(path: str | Path) -> dict[str, str]:
    """Parse key/value pairs from a desktop-entry style file.

    The parser intentionally keeps this lightweight: it ignores comments and
    group headers, preserves localized keys such as ``Name[zh_CN]``, and returns
    the last value seen for duplicate keys.
    """
    entry_path = Path(path).expanduser()
    fields: dict[str, str] = {}
    in_main_section = False
    has_any_section = False
    try:
        for raw_line in entry_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("["):
                has_any_section = True
                if line == "[Desktop Entry]":
                    in_main_section = True
                elif in_main_section:
                    break  # Stop at the next section after [Desktop Entry]
                continue
            # Accept key=value lines in the main section, or when no
            # section headers exist at all (simple key=value files).
            if (not in_main_section and has_any_section) or "=" not in line:
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

    When the first program is a generic launcher, its own name is not the
    app's, so this looks past it:

    - ``env FOO=bar /opt/app`` skips ``env`` and its ``NAME=VALUE`` assignments
      and returns ``/opt/app`` -- unwrapping ``env`` is unambiguous.
    - a bare interpreter or wrapper (``python main.py``, ``sh -c '...'``,
      ``flatpak run org.example.App``, ``snap run foo``) returns ``""``: the
      real program is an argument this parser does not try to guess, and the
      launcher's name must never become a process or residue keyword. The app
      is still matched by its id/name tokens and, for flatpak/snap, its cgroup.

    Malformed quoted commands and an empty ``Exec=`` return ``""`` so callers
    can keep the entry instead of deleting something ambiguous.
    """
    exec_value = parse_desktop_entry(path).get("Exec", "")
    if not exec_value:
        return ""
    try:
        parts = shlex.split(exec_value)
    except ValueError:
        return ""
    tokens = [part for part in parts if not part.startswith("%")]
    if not tokens:
        return ""
    first = tokens[0]
    if Path(first).name not in _LAUNCHER_NAMES:
        return first
    if Path(first).name != "env":
        # A bare interpreter/wrapper: do not guess the inner program.
        return ""
    # env: skip it and any NAME=VALUE assignments, return the real program.
    for token in tokens[1:]:
        if "=" in token:
            continue
        return token
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
    if fields.get("NoDisplay", "").lower() == "true":
        return ""
    localized_key = f"Name[{locale}]" if locale else ""
    if localized_key and fields.get(localized_key):
        return fields[localized_key]
    return fields.get("Name", "")


def get_desktop_icon(path: str | Path) -> str:
    return parse_desktop_entry(path).get("Icon", "")
