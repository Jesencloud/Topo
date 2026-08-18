import os
import sys
from pathlib import Path

# Get version from root VERSION file
VERSION_FILE = Path(__file__).parent.parent.parent / "VERSION"
TOPO_VERSION = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "1.0.0"

# Search roots for user project directories (used by the __pycache__ cleaner)
DEFAULT_PROJECT_SEARCH_PATHS = [
    str(Path.home() / "Documents"),
    str(Path.home() / "Projects"),
    str(Path.home() / "Code"),
    str(Path.home() / "Development"),
    str(Path.home() / "src"),
    str(Path.home() / "repos"),
    str(Path.home() / "workspace"),
]

# Config files for detected paths
DETECTED_APPS_FILE = Path.home() / ".config" / "topo" / "detected_apps.json"

HOME = Path.home()

# Dev tool caches
DEV_CACHES = {
    "npm": HOME / ".npm",
    "pip": HOME / ".cache/pip",
    "cargo": HOME / ".cargo/registry",
    "go": HOME / ".cache/go-build",
}


# --- UI / ANSI Colors ---
BLUE: str = ""
CYAN: str = ""
MAGENTA: str = ""
YELLOW: str = ""
GREEN: str = ""
RED: str = ""
WHITE: str = ""
GRAY: str = ""
RESET: str = ""
BOLD: str = ""
PURPLE: str = ""
EARTH: str = ""
THEME_TITLE: str = ""
GREEN_NB: str = ""
GRAY_NB: str = ""
HIGHLIGHT: str = ""
OK: str = ""
SKIP: str = ""

# One place to retune the dimmed-text color. WHITE / GRAY / GRAY_NB all resolve
# to this; see the comment in _init_colors() for why they stay three names.
_NEUTRAL_GRAY = "\033[38;5;244m"

# Every name _init_colors() rebinds. Consumers do `from .constants import GREEN`,
# which copies the *value* into their own module namespace, so rebinding these
# globals alone is invisible to them (see _propagate_colors).
_COLOR_NAMES: tuple[str, ...] = (
    "BLUE",
    "CYAN",
    "MAGENTA",
    "YELLOW",
    "GREEN",
    "RED",
    "WHITE",
    "GRAY",
    "RESET",
    "BOLD",
    "PURPLE",
    "EARTH",
    "THEME_TITLE",
    "GREEN_NB",
    "GRAY_NB",
    "HIGHLIGHT",
    "OK",
    "SKIP",
)


def _propagate_colors() -> None:
    """Push the current color values into modules that from-imported them.

    ``setup_color_mode()`` runs after argparse, i.e. long after every UI module
    has already bound its own copy of GREEN/RESET/... at import time. Without
    this rebind, ``--no-color`` would silently do nothing: only NO_COLOR and the
    non-TTY case work by accident, because those are decided during this
    module's own import, before the from-imports happen.
    """
    root = __name__.split(".")[0]
    values = {name: globals()[name] for name in _COLOR_NAMES}
    for mod_name, module in list(sys.modules.items()):
        if module is None or mod_name == __name__:
            continue
        if mod_name != root and not mod_name.startswith(f"{root}."):
            continue
        for name, value in values.items():
            if hasattr(module, name):
                setattr(module, name, value)


def _init_colors(disable: bool = False):
    global \
        BLUE, \
        CYAN, \
        MAGENTA, \
        YELLOW, \
        GREEN, \
        RED, \
        WHITE, \
        GRAY, \
        RESET, \
        BOLD, \
        PURPLE, \
        EARTH, \
        THEME_TITLE
    global GREEN_NB, GRAY_NB, HIGHLIGHT, OK, SKIP

    if disable:
        BLUE = CYAN = MAGENTA = YELLOW = GREEN = RED = WHITE = GRAY = RESET = BOLD = PURPLE = (
            EARTH
        ) = THEME_TITLE = ""
        GREEN_NB = GRAY_NB = HIGHLIGHT = ""
        OK = "✓"
        SKIP = "◎"
    else:
        BLUE = "\033[1;34m"
        CYAN = "\033[1;36m"
        MAGENTA = "\033[1;35m"
        YELLOW = "\033[1;33m"
        GREEN = "\033[1;32m"
        RED = "\033[1;31m"
        # WHITE, GRAY and GRAY_NB are deliberately the same neutral mid-gray:
        # high contrast on both dark and light backgrounds. They are kept as
        # three names because they mark three different intents -- WHITE dims a
        # value or an empty bar track, GRAY dims secondary text, GRAY_NB is the
        # non-bold twin of GREEN_NB used by the SKIP glyph -- so any one of them
        # can be retuned later without disturbing the other two. If you are
        # comparing them expecting different escapes, they are not different yet.
        WHITE = GRAY = GRAY_NB = _NEUTRAL_GRAY
        RESET = "\033[0m"
        BOLD = "\033[1m"
        PURPLE = "\033[1;95m"
        EARTH = YELLOW  # Adaptive Bold Yellow (Matches theme #8B8B00 across palettes)
        THEME_TITLE = PURPLE

        GREEN_NB = "\033[0;32m"

        # Selected chip in confirmation dialogs: bright text on a magenta field.
        HIGHLIGHT = "\033[1;37m\033[45m"

        OK = f"{GREEN_NB}✓{RESET}"
        SKIP = f"{GRAY_NB}◎{RESET}"


def setup_color_mode(no_color: bool = False) -> None:
    """Configures ANSI colors according to --no-color flag and NO_COLOR env spec (https://no-color.org/)."""
    env_no_color = bool(os.environ.get("NO_COLOR", "").strip())
    disable = no_color or env_no_color or not sys.stdout.isatty()
    _init_colors(disable=disable)
    _propagate_colors()


# Auto-initialize based on NO_COLOR environment specification at module import
setup_color_mode()

# Terminal control sequences
CLEAR_SCREEN = "\033[2J\033[H"
CLEAR_LINE = "\r\033[K"
ERASE_BELOW = "\033[J"

# Age-based cleanup thresholds (days)
CLEAN_CACHE_AGE_DAYS = 30
CLEAN_CARGO_AGE_DAYS = 7
CLEAN_TEMP_AGE_DAYS = 3

# Time conversion
SECONDS_PER_DAY = 86400

# Batch processing
RPM_QUERY_BATCH_SIZE = 500

# SQLite progress handler callback interval (virtual-machine instructions)
SQLITE_PROGRESS_INTERVAL = 10000
