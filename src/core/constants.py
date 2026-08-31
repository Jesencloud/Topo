import os
import sys
from enum import Enum
from pathlib import Path

from .paths import get_config_dir

# The VERSION file at the install root, and the only place it is read. Every
# component used to read it itself and invent its own answer for "unreadable"
# (1.0.0 here, 0.0.0 in the updater, "Unavailable" in doctor), so a missing
# VERSION made `topo --version`, `topo doctor` and `topo update` disagree about
# which version was installed -- and the updater's 0.0.0 made every remote tag
# look newer, turning a lost file into an unrequested reinstall.
VERSION_FILE = Path(__file__).parent.parent.parent / "VERSION"

# What TOPO_VERSION says when the file cannot be read. Deliberately not a version
# number: it has to be honest in `topo --version` output and it must not parse as
# a Version, so `topo update` refuses to compare instead of guessing.
UNKNOWN_VERSION = "unknown"

# The comment `topo link` writes above the PATH export it appends to a shell rc
# file, and the line `topo remove` looks for to take that block back out again.
# One spelling in one place: the two commands used to write and match their own
# copies, so a reworded marker would have left every existing installation's PATH
# entry behind forever, with `topo remove` reporting it as removed.
TOPO_RC_MARKER = "# Added by topo"


def read_topo_version(version_file: Path | None = None) -> str | None:
    """The version of this topo copy, or None when VERSION cannot be read.

    Callers differ in how they present "cannot be read" -- doctor names it as a
    problem, the updater refuses to compare, the banner falls back to
    UNKNOWN_VERSION -- but they no longer differ on what it means. *version_file*
    is only passed by doctor, which reports on the tree at get_install_root()
    rather than on this module's own; the two are the same directory (pinned by
    tests/test_single_sources.py).

    Bytes rather than read_text(): this runs at import time, so a VERSION file
    whose bytes are not UTF-8 -- a botched write, a truncated download, a tree
    unpacked with the wrong tool -- used to raise UnicodeDecodeError before main()
    existed to catch anything, and *every* topo command ended in a traceback. A
    file that does not decode is a file that cannot be read, which this function
    already has one answer for, so it joins missing, empty and whitespace-only.
    Not errors="replace": the value goes on to be compared against a release tag,
    and "1.2.\\ufffd" is a wrong version rather than an absent one.
    """
    try:
        version = (version_file or VERSION_FILE).read_bytes().decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    return version or None


TOPO_VERSION: str = read_topo_version() or UNKNOWN_VERSION

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
DETECTED_APPS_FILE = get_config_dir() / "detected_apps.json"

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

# --- Status glyphs ---
#
# The whole vocabulary, in one place. Every one of them carries its own color and
# its own {RESET}, so the sentence after it stays uncolored: a report line reads
# `f"{OK} Removed {name}"`, never `f"{GREEN}✓ Removed {name}{RESET}"`. Only a
# label word that genuinely needs emphasis (`Failed:`) may be colored, and then
# as its own segment outside the glyph.
#
# Six states, and the two dim ones are not the same state:
#   OK    something was done
#   FAIL  something was attempted and did not work
#   WARN  something needs attention but is not a failure
#   INFO  an aside -- context, not an outcome
#   SKIP  it exists, this run did not touch it (dry-run, whitelisted, running app)
#   NA    it does not exist on this machine, so there was nothing to do
# Reaching for SKIP where NA belongs tells the reader a choice was made when the
# truth is there was nothing to choose.
#
# All six are single-column glyphs on purpose. ✅ / ❌ / ⚠️  are the double-wide
# emoji forms of ✓ / ✗ / ⚠, and one of them in a column of narrow glyphs shifts
# every following field by one cell -- which is why the emoji sites they replaced
# each carried a hand-tuned extra space.
OK: str = ""
FAIL: str = ""
WARN: str = ""
INFO: str = ""
SKIP: str = ""
NA: str = ""

# --- Leading marks ---
#
# What the line wants from you, not which module printed it. Colorless, because
# the color is the caller's (a header takes THEME_TITLE, a prompt takes PURPLE),
# and they are constants only so the three roles stay distinguishable:
MARK_SECTION = "➤"  # a heading: everything below belongs to it
MARK_PROMPT = "➔"  # this line is waiting for you to type or choose
MARK_NOTE = "●"  # an aside about the line, or the run, above

# The `====` rule a run's closing summary is boxed in. clean drew 60 and the
# uninstall report drew 70, so two summaries in one session had two widths. 70 is
# the wider of the two on purpose: the uninstall report lists application names,
# which are the longest thing either box has to hold, and a rule narrower than
# its content reads as a broken box rather than a tidy one.
SUMMARY_RULE_WIDTH = 70

# How wide a name column in the TUI is ever allowed to get. Four sites in
# ui/navigator.py wrote 35 by hand -- the analyze and top-files "Selected ... to
# Remove" lists, the uninstall table, and the uninstall list's own selection
# summary -- so widening one of them meant guessing whether the other three were
# the same intent. They are: every one of them is a name column that the rest of
# its row is measured around.
#
# 35 is also exactly what makes two of those columns fit an 80-column terminal:
# each entry spends five cells on its "   • " prefix, and 2 * (5 + 35) = 80,
# which is the width at which the selection summary switches to two columns.
NAME_COLUMN_MAX_WIDTH = 35

# One place to retune the dimmed-text color. WHITE / GRAY / GRAY_NB all resolve
# to this; see the comment in _init_colors() for why they stay three names.
_NEUTRAL_GRAY = "\033[38;5;244m"

# Name of the configured title color (config.json "theme_color"). Held here
# rather than read from core.config, so importing this module never touches the
# user's home directory: setup_color_mode() is handed the name by its caller.
_theme_color_name: str = "purple"


def _resolve_theme_title(name: str) -> str:
    """Map a theme_color name onto one of the palette escapes bound above.

    Called from _init_colors() after the palette is assigned, so --no-color
    still wins: it never reaches this at all.
    """
    return {
        "purple": PURPLE,
        "cyan": CYAN,
        "blue": BLUE,
        "magenta": MAGENTA,
        "green": GREEN,
        "yellow": YELLOW,
        "red": RED,
    }.get(name, PURPLE)


# Every name _init_colors() rebinds. Consumers do `from .constants import GREEN`,
# which copies the *value* into their own module namespace, so rebinding these
# globals alone is invisible to them (see _propagate_constants).
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
    "FAIL",
    "WARN",
    "INFO",
    "SKIP",
    "NA",
)

# Terminal control sequences. Rebound by _init_terminal_control() and pushed out
# by the same propagation pass as the colors, for the same reason.
_TERMINAL_CONTROL_NAMES: tuple[str, ...] = ("CLEAR_SCREEN", "CLEAR_LINE", "ERASE_BELOW")

_PROPAGATED_NAMES: tuple[str, ...] = _COLOR_NAMES + _TERMINAL_CONTROL_NAMES


def _propagate_constants() -> None:
    """Push the current color and terminal-control values into modules that from-imported them.

    ``setup_color_mode()`` runs after argparse, i.e. long after every UI module
    has already bound its own copy of GREEN/RESET/... at import time. Without
    this rebind, ``--no-color`` would silently do nothing: only NO_COLOR and the
    non-TTY case work by accident, because those are decided during this
    module's own import, before the from-imports happen.
    """
    root = __name__.split(".")[0]
    values = {name: globals()[name] for name in _PROPAGATED_NAMES}
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
    global GREEN_NB, GRAY_NB, HIGHLIGHT, OK, FAIL, WARN, INFO, SKIP, NA

    if disable:
        BLUE = CYAN = MAGENTA = YELLOW = GREEN = RED = WHITE = GRAY = RESET = BOLD = PURPLE = (
            EARTH
        ) = THEME_TITLE = ""
        GREEN_NB = GRAY_NB = HIGHLIGHT = ""
        OK = "✓"
        FAIL = "✗"
        WARN = "⚠"
        INFO = "ℹ"
        SKIP = "◎"
        NA = "-"
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
        # non-bold twin of GREEN_NB used by the dim glyphs (INFO / SKIP / NA) --
        # so any one of them can be retuned later without disturbing the other
        # two. If you are comparing them expecting different escapes, they are
        # not different yet.
        WHITE = GRAY = GRAY_NB = _NEUTRAL_GRAY
        RESET = "\033[0m"
        BOLD = "\033[1m"
        PURPLE = "\033[1;95m"
        EARTH = YELLOW  # Adaptive Bold Yellow (Matches theme #8B8B00 across palettes)
        THEME_TITLE = _resolve_theme_title(_theme_color_name)

        GREEN_NB = "\033[0;32m"

        # Selected chip in confirmation dialogs: bright text on a magenta field.
        HIGHLIGHT = "\033[1;37m\033[45m"

        OK = f"{GREEN_NB}✓{RESET}"
        FAIL = f"{RED}✗{RESET}"
        WARN = f"{YELLOW}⚠{RESET}"
        # Dim on purpose: an aside must not compete with the outcome lines around
        # it, and every INFO line's own text is GRAY for the same reason.
        INFO = f"{GRAY_NB}ℹ{RESET}"
        SKIP = f"{GRAY_NB}◎{RESET}"
        NA = f"{GRAY_NB}-{RESET}"


# Terminal control sequences.
#
# These are gated on isatty() *alone*, deliberately not on the color switch:
# --no-color and NO_COLOR ask topo to stop colouring, not to stop driving the
# terminal. Someone who exports NO_COLOR=1 still wants `topo analyze` to repaint
# its frame in place rather than smear every frame down the scrollback.
#
# What makes them meaningless is the absence of a terminal. A pipe or a file has
# no screen to clear and no line to rewind, and before this gate existed
# `topo optimize > log` wrote \033[2J plus a spinner frame's \r\033[K a dozen
# times a second into the log -- the colors were correctly gone, the cursor
# control was not.
#
# Declared empty like the colors above: the real values live only in
# _init_terminal_control(), which the import-time setup_color_mode() call runs
# before any consumer can from-import them.
CLEAR_SCREEN: str = ""
CLEAR_LINE: str = ""
ERASE_BELOW: str = ""


def _init_terminal_control(disable: bool = False) -> None:
    global CLEAR_SCREEN, CLEAR_LINE, ERASE_BELOW

    if disable:
        CLEAR_SCREEN = CLEAR_LINE = ERASE_BELOW = ""
    else:
        CLEAR_SCREEN = "\033[2J\033[H"
        CLEAR_LINE = "\r\033[K"
        ERASE_BELOW = "\033[J"


def setup_color_mode(no_color: bool = False, theme_color: str | None = None) -> None:
    """Configures ANSI colors according to --no-color flag and NO_COLOR env spec (https://no-color.org/).

    *theme_color* is the config.json "theme_color" name; None keeps whatever was
    set last (the default at import, so the automatic call below stays quiet).

    Terminal control sequences are re-evaluated here too, but against a narrower
    condition -- see the comment above ``_init_terminal_control``.
    """
    global _theme_color_name
    if theme_color:
        _theme_color_name = theme_color
    env_no_color = bool(os.environ.get("NO_COLOR", "").strip())
    is_terminal = sys.stdout.isatty()
    disable = no_color or env_no_color or not is_terminal
    _init_colors(disable=disable)
    _init_terminal_control(disable=not is_terminal)
    _propagate_constants()


# Auto-initialize based on NO_COLOR environment specification at module import
setup_color_mode()


# Age-based cleanup thresholds (days)
CLEAN_CACHE_AGE_DAYS = 30
CLEAN_CARGO_AGE_DAYS = 7
CLEAN_TEMP_AGE_DAYS = 3
# Editor backups and swap files are user documents, so they get a wider window
# than scratch data: long enough that an open editor session can never fall
# inside it, short enough to still be worth reclaiming.
CLEAN_BACKUP_AGE_DAYS = 7

# The one spelling of an hour and of a day in seconds. Four sites wrote 86400 by
# hand while SECONDS_PER_DAY was sitting here being imported by a fifth, and 3600
# was written out at all four of its call sites, so a guard in
# tests/test_single_sources.py now fails on either literal anywhere but these two
# lines. Only these two: minutes are not here because 60 also means "sixty
# seconds" as a subprocess timeout in a dozen places, and a name that has to be
# right for both meanings is worth less than the digits.
SECONDS_PER_HOUR = 3600
SECONDS_PER_DAY = 86400

# Batch processing
RPM_QUERY_BATCH_SIZE = 500

# SQLite progress handler callback interval (virtual-machine instructions)
SQLITE_PROGRESS_INTERVAL = 10000


class AppType(str, Enum):
    """Where `topo uninstall` found an application, and therefore what removes it.

    These eight words used to be written out as bare strings at 31 sites -- 22 in
    uninstall.py, five in the removal screen, four in the distro matrix -- with no
    definition anywhere: the scanners stamped one onto every record they built, the
    removal dispatched on it eleven times, and the screen kept its own frozenset of
    the five that need root. A misspelling was not an error, only a package that
    quietly reached the "unsupported package type" branch, and adding a ninth
    manager meant finding all 31 by memory.

    A ``str`` subclass, so a member stays interchangeable with the word it spells:
    ``AppType.FLATPAK == "Flatpak"`` and the two hash alike, which is what lets a
    record built by hand -- every test does -- go on being compared against these
    members. ``enum.StrEnum`` says exactly this, but it arrived in 3.11 and the
    supported floor is 3.10 (install.sh), so the mixin is spelled out; ``__str__``
    is the half of StrEnum that matters here, because a plain ``str, Enum`` member
    renders as "AppType.APT" in an f-string from 3.11 on, and these words are
    printed ("Cleaned APT cache").
    """

    FLATPAK = "Flatpak"
    SNAP = "Snap"
    NPM = "NPM"
    CLI = "CLI"
    APT = "APT"
    DNF = "DNF"
    ZYPPER = "Zypper"
    PACMAN = "Pacman"

    def __str__(self) -> str:
        return self.value
