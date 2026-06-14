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
BLUE = "\033[1;34m"
CYAN = "\033[1;36m"
MAGENTA = "\033[1;35m"
YELLOW = "\033[1;33m"
GREEN = "\033[1;32m"
RED = "\033[1;31m"
WHITE = "\033[38;5;244m"  # Dark gray (visible on both black and white backgrounds)
GRAY = "\033[1;90m"
RESET = "\033[0m"
BOLD = "\033[1m"
PURPLE = "\033[1;95m"
EARTH = "\033[38;5;100m"  # Yellow4 / Olive (Matches logo #8B8B00)
THEME_TITLE = PURPLE
