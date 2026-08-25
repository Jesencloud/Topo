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
