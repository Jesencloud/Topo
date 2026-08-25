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

    install.sh needs the same answer before it runs `topo link`, but it must not
    import this function: the script comes from main while the tree it installs
    is whichever release was requested, so an import would bind the installer to
    that release's API and break every older version. It reimplements the three
    branches in shell (`resolve_link_target_dir`), and
    tests/test_install.py diffs the two implementations on every branch --
    changing the rule here means changing that function in the same commit.

    A relative TOPO_LINK_DIR is deliberately left relative (expanduser, not
    resolve): `topo link` runs with the install tree as its working directory, so
    a relative override lands under ~/.topo, and install.sh mirrors that.
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
