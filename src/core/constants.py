import os
import sys
from pathlib import Path

# Get version from root VERSION file
VERSION_FILE = Path(__file__).parent.parent.parent / "VERSION"
TOPO_VERSION = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else "1.0.0"

# Canonical purge targets (heavy project build artifacts)
PURGE_TARGETS = {
    "node_modules",
    "target",  # Rust, Maven
    "build",  # Gradle, various
    "dist",  # JS builds
    "venv",  # Python
    ".venv",  # Python
    ".pytest_cache",  # Python (pytest)
    ".mypy_cache",  # Python (mypy)
    ".tox",  # Python (tox virtualenvs)
    ".nox",  # Python (nox virtualenvs)
    ".ruff_cache",  # Python (ruff)
    ".gradle",  # Gradle local
    "__pycache__",  # Python
    ".next",  # Next.js
    ".nuxt",  # Nuxt.js
    ".output",  # Nuxt.js
    "vendor",  # PHP Composer
    "bin",  # .NET build output (guarded)
    "obj",  # C# / Unity
    ".turbo",  # Turborepo cache
    ".parcel-cache",  # Parcel bundler
    ".dart_tool",  # Flutter/Dart build cache
    ".zig-cache",  # Zig
    "zig-out",  # Zig
    ".angular",  # Angular
    ".svelte-kit",  # SvelteKit
    ".astro",  # Astro
    "coverage",  # Code coverage reports
    ".cxx",  # React Native Android NDK build cache
    ".expo",  # Expo
    ".build",  # Swift Package Manager
}

# Monorepo indicators (higher priority)
MONOREPO_INDICATORS = {
    "lerna.json",
    "pnpm-workspace.yaml",
    "nx.json",
    "rush.json",
}

# Project indicators for container detection
PROJECT_INDICATORS = {
    "package.json",
    "Cargo.toml",
    "go.mod",
    "pyproject.toml",
    "requirements.txt",
    "pom.xml",
    "build.gradle",
    "Gemfile",
    "composer.json",
    "pubspec.yaml",
    "Package.swift",
    "Makefile",
    "build.zig",
    "build.zig.zon",
    ".git",
}

# Default search paths for Linux
DEFAULT_PURGE_SEARCH_PATHS = [
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
OK: str = ""
SKIP: str = ""
FAIL: str = ""


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
    global GREEN_NB, GRAY_NB, OK, SKIP, FAIL

    if disable:
        BLUE = CYAN = MAGENTA = YELLOW = GREEN = RED = WHITE = GRAY = RESET = BOLD = PURPLE = (
            EARTH
        ) = THEME_TITLE = ""
        GREEN_NB = GRAY_NB = ""
        OK = "✓"
        SKIP = "◎"
        FAIL = "✗"
    else:
        BLUE = "\033[1;34m"
        CYAN = "\033[1;36m"
        MAGENTA = "\033[1;35m"
        YELLOW = "\033[1;33m"
        GREEN = "\033[1;32m"
        RED = "\033[1;31m"
        WHITE = (
            "\033[38;5;244m"  # Neutral mid-gray (high contrast on both dark and light backgrounds)
        )
        GRAY = "\033[38;5;244m"  # High-contrast neutral gray for multi-theme support
        RESET = "\033[0m"
        BOLD = "\033[1m"
        PURPLE = "\033[1;95m"
        EARTH = "\033[38;5;100m"
        THEME_TITLE = PURPLE

        GREEN_NB = "\033[0;32m"
        GRAY_NB = "\033[38;5;244m"

        OK = f"{GREEN_NB}✓{RESET}"
        SKIP = f"{GRAY_NB}◎{RESET}"
        FAIL = f"{RED}✗{RESET}"


def setup_color_mode(no_color: bool = False) -> None:
    """Configures ANSI colors according to --no-color flag and NO_COLOR env spec (https://no-color.org/)."""
    env_no_color = bool(os.environ.get("NO_COLOR", "").strip())
    disable = no_color or env_no_color or not sys.stdout.isatty()
    _init_colors(disable=disable)


# Auto-initialize based on NO_COLOR environment specification at module import
setup_color_mode()

# Terminal control sequences
CLEAR_SCREEN = "\033[2J\033[H"
CLEAR_LINE = "\r\033[K"
ERASE_BELOW = "\033[J"

# Age-based cleanup thresholds (days)
CLEAN_CACHE_AGE_DAYS = 30
CLEAN_ORPHAN_AGE_DAYS = 60
CLEAN_CARGO_AGE_DAYS = 7
CLEAN_TEMP_AGE_DAYS = 3

# Time conversion
SECONDS_PER_DAY = 86400

# Batch processing
RPM_QUERY_BATCH_SIZE = 500

# SQLite progress handler callback interval (virtual-machine instructions)
SQLITE_PROGRESS_INTERVAL = 10000
