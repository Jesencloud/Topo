import os
from pathlib import Path


def get_config_dir() -> Path:
    return Path.home() / ".config" / "topo"


def get_state_dir() -> Path:
    """Topo's own XDG state directory — deletion history and other run state.

    The one place the XDG_STATE_HOME fallback is spelled out. It used to be
    derived three times (twice in `topo remove`, once by the audit log that
    creates it), so the command that deletes the directory and the code that
    writes it could have disagreed about where it is.
    """
    return Path(os.environ.get("XDG_STATE_HOME", "~/.local/state")).expanduser() / "topo"


def get_link_target_dir() -> Path:
    """Where the `topo` launcher symlink belongs: TOPO_LINK_DIR, or a default.

    PUBLIC CROSS-LANGUAGE CONTRACT: install.sh imports this function by name to
    resolve the launcher path before running `topo link`, so renaming it or
    changing what it returns means editing install.sh in the same commit.
    tests/test_install.py runs the script's own snippet to keep that honest --
    nothing else would catch it, since ruff/mypy/vulture/tach never read shell
    strings.

    A relative TOPO_LINK_DIR is deliberately left relative (expanduser, not
    resolve): install.sh runs `topo link` with the install tree as its working
    directory, so a relative override lands under ~/.topo, and the script's own
    fast path mirrors that.
    """
    if override := os.environ.get("TOPO_LINK_DIR"):
        return Path(override).expanduser()
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return Path("/usr/local/bin")
    return Path.home() / ".local" / "bin"


def get_launcher_candidates() -> list[Path]:
    """Every path a topo launcher could occupy, most likely first, deduplicated.

    `topo remove` scans all of them rather than only get_link_target_dir(): the
    install may have run with a different TOPO_LINK_DIR, or as root, and a
    launcher left behind is a dangling `topo` on PATH -- the next invocation
    reports "No such file or directory" instead of a removed program. ~ goes
    through Path.home() rather than expanduser() so every topo path agrees on one
    notion of home.
    """
    return list(
        dict.fromkeys(
            [
                get_link_target_dir() / "topo",
                Path.home() / ".local" / "bin" / "topo",
                Path("/usr/local/bin/topo"),
            ]
        )
    )
