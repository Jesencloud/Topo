#!/usr/bin/env bash

set -e

# ANSI colors
CYAN='\033[1;36m'
GREEN='\033[1;32m'
YELLOW='\033[1;33m'
RED='\033[1;31m'
GRAY='\033[1;90m'
NC='\033[0m' # No Color
BOLD='\033[1m'

MINIMAL=false
TARGET_REF=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --minimal)
            MINIMAL=true
            shift
            ;;
        --version|--ref)
            if [[ -z "${2:-}" ]]; then
                echo -e "${RED}✗ Error: $1 requires a version/tag value.${NC}"
                exit 1
            fi
            TARGET_REF="$2"
            shift 2
            ;;
        *)
            echo -e "${RED}✗ Error: unknown installer option '$1'.${NC}"
            exit 1
            ;;
    esac
done

# 1. Check prerequisites
if [ "$MINIMAL" = false ]; then
    echo -e "${CYAN}☉ Checking prerequisites...${NC}"
fi

HAS_GIT=false
if command -v git >/dev/null 2>&1; then
    if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓ git installed${NC}"; fi
    HAS_GIT=true
else
    if [ "$MINIMAL" = false ]; then echo -e "  ${YELLOW}ℹ git not found, will use direct download fallback${NC}"; fi
fi

command -v python3 >/dev/null 2>&1 || { echo -e "  ${RED}✗ Error: python3 is required but not installed.${NC}"; exit 1; }
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓ python3 installed${NC}"; fi
if ! python3 -c "import packaging" >/dev/null 2>&1; then
    echo -e "  ${RED}✗ Error: Python package 'packaging' is required but not installed.${NC}"
    echo -e "  ${GRAY}Install it with one of:${NC}"
    echo -e "    ${BOLD}sudo apt install python3-packaging${NC}        ${GRAY}# Debian/Ubuntu${NC}"
    echo -e "    ${BOLD}sudo dnf install python3-packaging${NC}        ${GRAY}# Fedora/RHEL${NC}"
    echo -e "    ${BOLD}sudo pacman -S python-packaging${NC}           ${GRAY}# Arch/Manjaro${NC}"
    exit 1
fi
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓ python packaging installed${NC}"; fi

if [ -z "$TARGET_REF" ]; then
    if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↺ Resolving latest stable release...${NC}"; fi
    TARGET_REF=$(python3 - <<'PY'
import json
import sys
import urllib.request

try:
    with urllib.request.urlopen(
        "https://api.github.com/repos/Jesencloud/Topo/releases/latest",
        timeout=15,
    ) as response:
        tag = json.load(response).get("tag_name", "")
except Exception:
    tag = ""

if not isinstance(tag, str) or not tag.strip():
    sys.exit(1)
print(tag.strip())
PY
    ) || {
        echo -e "  ${RED}✗ Error: failed to resolve the latest Topo release.${NC}"
        echo -e "  ${GRAY}Install a specific version with:${NC} ${BOLD}bash install.sh --version v0.6.0${NC}"
        echo -e "  ${GRAY}Install the development branch with:${NC} ${BOLD}bash install.sh --ref main${NC}"
        exit 1
    }
fi
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓ target release ${TARGET_REF}${NC}"; fi

# 2. Define paths
INSTALL_DIR="$HOME/.topo"
REPO_URL="https://github.com/Jesencloud/Topo.git"
if [ "$TARGET_REF" = "main" ]; then
    TARBALL_URL="https://github.com/Jesencloud/Topo/archive/refs/heads/main.tar.gz"
else
    TARBALL_URL="https://github.com/Jesencloud/Topo/archive/refs/tags/${TARGET_REF}.tar.gz"
fi
WAS_INSTALLED=false

# 3. Clone or download source
if [ "$MINIMAL" = false ]; then
    echo -e "\n${CYAN}☉ Fetching Topo...${NC}"
fi

if [ "$HAS_GIT" = true ]; then
    if [ -d "$INSTALL_DIR" ]; then
        WAS_INSTALLED=true
        if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↺ Updating Topo in ${INSTALL_DIR} (${TARGET_REF})...${NC}"; fi
        cd "$INSTALL_DIR"
        if [ -d ".git" ]; then
            # To keep things clean, we reset and pull
            git fetch --quiet --depth 1 origin "$TARGET_REF"
            git reset --hard FETCH_HEAD --quiet
        else
            if [ "$MINIMAL" = false ]; then echo -e "  ${YELLOW}⚠ Existing install is not a git checkout; reinstalling cleanly.${NC}"; fi
            cd "$HOME"
            rm -rf "$INSTALL_DIR"
            git clone --quiet --depth 1 --branch "$TARGET_REF" "$REPO_URL" "$INSTALL_DIR"
            WAS_INSTALLED=false
        fi
    else
        if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↓ Downloading Topo via Git (${TARGET_REF})...${NC}"; fi
        git clone --quiet --depth 1 --branch "$TARGET_REF" "$REPO_URL" "$INSTALL_DIR"
    fi
else
    if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↓ Downloading Topo archive (${TARGET_REF})...${NC}"; fi
    mkdir -p "$INSTALL_DIR"
    # Download and extract, stripping the top-level directory (Topo-main)
    curl -fsSL "$TARBALL_URL" | tar -xzC "$INSTALL_DIR" --strip-components=1
    # Mark as non-git install for update logic
    touch "$INSTALL_DIR/.non_git_install"
fi

# 4. Clean up and provision binaries
if [ "$MINIMAL" = false ]; then
    echo -e "  ${GRAY}🧹 Refining installation directory...${NC}"
fi
cd "$INSTALL_DIR"

ARCH=$(uname -m)
BIN_DIR="src/core/bin"
if [ "$TARGET_REF" = "main" ]; then
    RELEASE_URL="https://github.com/Jesencloud/Topo/releases/latest/download"
else
    RELEASE_URL="https://github.com/Jesencloud/Topo/releases/download/${TARGET_REF}"
fi

# Ensure binary directory exists
mkdir -p "$BIN_DIR"

if [[ "$ARCH" == "x86_64" ]]; then
    if [ ! -f "$BIN_DIR/topo-core-x86_64" ]; then
        if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↓ Fetching x86_64 engine from ${TARGET_REF}...${NC}"; fi
        curl -fsSL "$RELEASE_URL/topo-core-x86_64" -o "$BIN_DIR/topo-core-x86_64" || echo -e "  ${RED}⚠ Warning: Could not download x86_64 engine.${NC}"
    else
        if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}Using bundled x86_64 engine.${NC}"; fi
    fi
    rm -f "$BIN_DIR/topo-core-aarch64"
elif [[ "$ARCH" == "aarch64" ]] || [[ "$ARCH" == "arm64" ]]; then
    if [ ! -f "$BIN_DIR/topo-core-aarch64" ]; then
        if [ "$MINIMAL" = false ]; then echo -e "  ${YELLOW}↓ ARM64 detected. Fetching optimized engine from ${TARGET_REF}...${NC}"; fi
        curl -fsSL "$RELEASE_URL/topo-core-aarch64" -o "$BIN_DIR/topo-core-aarch64" || echo -e "  ${RED}⚠ Warning: Could not download ARM64 engine.${NC}"
    else
        if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}Using bundled ARM64 engine.${NC}"; fi
    fi
    rm -f "$BIN_DIR/topo-core-x86_64"
fi
chmod +x $BIN_DIR/topo-core-* 2>/dev/null || true

# Keep LICENSE for compliance, but remove everything else non-essential
rm -rf tests/ daily_report.md pytest.ini topo.py .gitignore README.md topo-core/

# 5. Run the linking script
if [ "$MINIMAL" = false ]; then
    echo -e "\n${CYAN}☉ Configuring system...${NC}"
fi
chmod +x topo

# Pass --silent if this was an update to avoid redundant success banners
if [ "$WAS_INSTALLED" = true ]; then
    ./topo link --silent
else
    ./topo link
fi

if ! command -v topo >/dev/null 2>&1; then
    echo -e "  ${YELLOW}⚠ Warning: 'topo' is not available in PATH yet.${NC}"
    echo -e "  ${GRAY}You can run it directly with:${NC} ${BOLD}${INSTALL_DIR}/topo${NC}"
    echo -e "  ${GRAY}Or create a link manually, for example:${NC} ${BOLD}sudo ln -sf ${INSTALL_DIR}/topo /usr/local/bin/topo${NC}"
fi

# 6. Display final banner and version
if [ "$MINIMAL" = false ]; then
    # Extract version
    TOPO_VER="unknown"
    if [ -f "VERSION" ]; then
        TOPO_VER=$(cat VERSION)
    fi

    echo -e "${CYAN}"
    echo "  ████████  ██████  ██████   ██████ "
    echo "     ██    ██    ██ ██   ██ ██    ██"
    echo "     ██    ██    ██ ██████  ██    ██"
    echo "     ██    ██    ██ ██      ██    ██"
    echo "     ██     ██████  ██       ██████ "
    echo -e "${NC}"
    echo -e " ${CYAN}●${NC} ${BOLD}Topo v${TOPO_VER}${NC} ${GRAY}is digging deeper 🦡 🦡 🦡${NC}\n"
    
    echo -e "${GRAY}Type '${NC}topo${GRAY}' to start the interactive TUI, or '${NC}topo --help${GRAY}' to explore all commands.${NC}"
fi
