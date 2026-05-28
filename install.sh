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
if [[ "$1" == "--minimal" ]]; then
    MINIMAL=true
fi

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
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓ python3 installed${NC}\n"; fi

if [ "$MINIMAL" = false ]; then
    echo -e "${CYAN}"
    echo "  ████████  ██████  ██████   ██████ "
    echo "     ██    ██    ██ ██   ██ ██    ██"
    echo "     ██    ██    ██ ██████  ██    ██"
    echo "     ██    ██    ██ ██      ██    ██"
    echo "     ██     ██████  ██       ██████ "
    echo -e "${NC}"
    echo -e " ${CYAN}●${NC} ${BOLD}Topo${NC} ${GRAY}is digging deeper 🦡 🦡 🦡${NC}\n"
fi

# 2. Define paths
INSTALL_DIR="$HOME/.topo"
REPO_URL="https://github.com/Jesencloud/Topo.git"
TARBALL_URL="https://github.com/Jesencloud/Topo/archive/refs/heads/main.tar.gz"

# 3. Clone or download source
if [ "$MINIMAL" = false ]; then
    echo -e "\n${CYAN}☉ Fetching Topo...${NC}"
fi

if [ "$HAS_GIT" = true ]; then
    if [ -d "$INSTALL_DIR" ]; then
        if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↺ Updating Topo in ${INSTALL_DIR}...${NC}"; fi
        cd "$INSTALL_DIR"
        # To keep things clean, we reset and pull
        git fetch --quiet --depth 1 origin main
        git reset --hard origin/main --quiet
    else
        if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↓ Downloading Topo via Git...${NC}"; fi
        git clone --quiet --depth 1 "$REPO_URL" "$INSTALL_DIR"
    fi
else
    if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↓ Downloading Topo archive...${NC}"; fi
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
# Point to the latest release assets
RELEASE_URL="https://github.com/Jesencloud/Topo/releases/latest/download"

# Ensure binary directory exists
mkdir -p "$BIN_DIR"

if [[ "$ARCH" == "x86_64" ]]; then
    if [ ! -f "$BIN_DIR/topo-core-x86_64" ]; then
        if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↓ Fetching x86_64 engine from latest release...${NC}"; fi
        curl -fsSL "$RELEASE_URL/topo-core-x86_64" -o "$BIN_DIR/topo-core-x86_64" || echo -e "  ${RED}⚠ Warning: Could not download x86_64 engine.${NC}"
    else
        if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}Using bundled x86_64 engine.${NC}"; fi
    fi
    rm -f "$BIN_DIR/topo-core-aarch64"
elif [[ "$ARCH" == "aarch64" ]] || [[ "$ARCH" == "arm64" ]]; then
    if [ ! -f "$BIN_DIR/topo-core-aarch64" ]; then
        if [ "$MINIMAL" = false ]; then echo -e "  ${YELLOW}↓ ARM64 detected. Fetching optimized engine from latest release...${NC}"; fi
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
if [ -d "$INSTALL_DIR" ]; then
    ./topo link --silent
else
    ./topo link
fi

# Note: The ./topo link command already prints the success message.
if [ "$MINIMAL" = false ]; then
    echo -e "\n${GRAY}Type '${NC}topo${GRAY}' to start the interactive TUI, or '${NC}topo --help${GRAY}' to explore all commands.${NC}"
fi
