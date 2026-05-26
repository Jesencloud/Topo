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

echo -e "${CYAN}"
echo "  ████████  ██████  ██████   ██████ "
echo "     ██    ██    ██ ██   ██ ██    ██"
echo "     ██    ██    ██ ██████  ██    ██"
echo "     ██    ██    ██ ██      ██    ██"
echo "     ██     ██████  ██       ██████ "
echo -e "${NC}"
echo -e " ${CYAN}●${NC} ${BOLD}Topo${NC} ${GRAY}is digging deeper 🦡 🦡 🦡${NC}\n"

# 1. Check prerequisites
echo -e "${CYAN}☉ Checking prerequisites...${NC}"
command -v git >/dev/null 2>&1 || { echo -e "  ${RED}✗ Error: git is required but not installed.${NC}"; exit 1; }
echo -e "  ${GREEN}✓ git installed${NC}"

command -v python3 >/dev/null 2>&1 || { echo -e "  ${RED}✗ Error: python3 is required but not installed.${NC}"; exit 1; }
echo -e "  ${GREEN}✓ python3 installed${NC}"

# 2. Define paths
INSTALL_DIR="$HOME/.topo"
REPO_URL="https://github.com/Jesencloud/Topo.git"

# 3. Clone or update repository
echo -e "\n${CYAN}☉ Fetching Topo...${NC}"
if [ -d "$INSTALL_DIR" ]; then
    echo -e "  ${GRAY}↺ Updating existing installation in ${INSTALL_DIR}...${NC}"
    cd "$INSTALL_DIR"
    git fetch --quiet origin main
    git reset --hard origin/main --quiet
else
    echo -e "  ${GRAY}↓ Downloading Topo to ${INSTALL_DIR}...${NC}"
    git clone --quiet "$REPO_URL" "$INSTALL_DIR"
fi

# 4. Run the linking script
echo -e "\n${CYAN}☉ Configuring system...${NC}"
cd "$INSTALL_DIR"
chmod +x topo
./topo link

# Note: The ./topo link command already prints the success message.
echo -e "\n${GRAY}Note: If you want to uninstall later, run '${NC}topo remove${GRAY}'${NC}"
