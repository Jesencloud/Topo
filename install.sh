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

if command -v git >/dev/null 2>&1; then
    if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓ git installed${NC}"; fi
else
    if [ "$MINIMAL" = false ]; then echo -e "  ${YELLOW}ℹ git not found (signed release installation is unaffected)${NC}"; fi
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

if [ "$MINIMAL" = false ]; then
    PACKAGE_REMOVE_COMMAND=""
    if command -v rpm >/dev/null 2>&1 && rpm -q topo >/dev/null 2>&1; then
        PACKAGE_REMOVE_COMMAND="sudo dnf remove topo"
    elif command -v dpkg-query >/dev/null 2>&1 && \
        dpkg-query -W -f='${Status}' topo 2>/dev/null | grep -q "install ok installed"; then
        PACKAGE_REMOVE_COMMAND="sudo apt remove topo"
    fi

    if [ -n "$PACKAGE_REMOVE_COMMAND" ]; then
        echo -e "  ${YELLOW}⚠ A system package install of Topo is still registered.${NC}"
        echo -e "  ${GRAY}It may shadow this script install through an old /usr/bin/topo path.${NC}"
        echo -e "  ${GRAY}Remove the package install with:${NC} ${BOLD}${PACKAGE_REMOVE_COMMAND}${NC}"
        echo -e "  ${GRAY}Then refresh your shell command cache with:${NC} ${BOLD}hash -r${NC}"
    fi
fi

if [ -z "$TARGET_REF" ]; then
    if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↺ Resolving latest stable release...${NC}"; fi
    TARGET_REF=$(
        curl -fsSLI -o /dev/null -w '%{url_effective}' \
            "https://github.com/Jesencloud/Topo/releases/latest" |
            sed 's#.*/##'
    )
    if [ -z "$TARGET_REF" ] || [ "$TARGET_REF" = "latest" ]; then
        TARGET_REF=$(python3 - <<'PY'
import json
import sys
import urllib.request

try:
    request = urllib.request.Request(
        "https://api.github.com/repos/Jesencloud/Topo/releases/latest",
        headers={"User-Agent": "topo-installer"},
    )
    with urllib.request.urlopen(
        request,
        timeout=15,
    ) as response:
        tag = json.load(response).get("tag_name", "")
except Exception:
    tag = ""

if not isinstance(tag, str) or not tag.strip():
    sys.exit(1)
print(tag.strip())
PY
        ) || true
    fi
    if [ -z "$TARGET_REF" ] || [ "$TARGET_REF" = "latest" ]; then
        echo -e "  ${RED}✗ Error: failed to resolve the latest Topo release.${NC}"
        echo -e "  ${GRAY}Install a specific version with:${NC} ${BOLD}bash install.sh --version v0.6.0${NC}"
        echo -e "  ${GRAY}Install the development branch with:${NC} ${BOLD}bash install.sh --ref main${NC}"
        exit 1
    fi
fi
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓ target release ${TARGET_REF}${NC}"; fi

# --- Release verification infrastructure (fail-closed) ---------------------
# Trust anchor: the release key fingerprint is pinned here, so a tampered
# SHA256SUMS is rejected even when the attacker also controls the manifest and
# the transport. Mirrors src/manage/update.py's TOPO_RELEASE_KEY_FINGERPRINT.
TOPO_KEY_FPR="4B35C17CF8E663732726A99F50086DB998B4D883"

if [ "$TARGET_REF" = "main" ]; then
    RELEASE_URL="https://github.com/Jesencloud/Topo/releases/latest/download"
else
    RELEASE_URL="https://github.com/Jesencloud/Topo/releases/download/${TARGET_REF}"
fi

VERIFY_DIR=""
SUMS_FILE=""
cleanup_verify_dir() {
    if [ -n "$VERIFY_DIR" ]; then rm -rf "$VERIFY_DIR"; fi
}
abort_verification() {
    echo -e "  ${RED}✗ ${1}${NC}" >&2
    echo -e "  ${GRAY}Refusing to install unverified files. Nothing was executed.${NC}" >&2
    exit 1
}

# Downloads and signature-verifies SHA256SUMS exactly once. Every failure path
# aborts: an attacker who suppresses the manifest must not be able to disable
# verification (the previous '|| true' made that a one-request bypass).
require_release_manifest() {
    if [ -n "$SUMS_FILE" ]; then return 0; fi
    if ! command -v gpg >/dev/null 2>&1; then
        echo -e "  ${RED}✗ gpg is required to verify release signatures but was not found.${NC}" >&2
        echo -e "  ${GRAY}Install it with one of:${NC}" >&2
        echo -e "    ${BOLD}sudo apt install gnupg${NC}     ${GRAY}# Debian/Ubuntu${NC}" >&2
        echo -e "    ${BOLD}sudo dnf install gnupg2${NC}    ${GRAY}# Fedora/RHEL${NC}" >&2
        echo -e "    ${BOLD}sudo pacman -S gnupg${NC}       ${GRAY}# Arch/Manjaro${NC}" >&2
        exit 1
    fi
    VERIFY_DIR=$(mktemp -d)
    curl -fsSL "$RELEASE_URL/SHA256SUMS" -o "$VERIFY_DIR/SHA256SUMS" ||
        abort_verification "Could not download the SHA256SUMS manifest."
    curl -fsSL "$RELEASE_URL/SHA256SUMS.asc" -o "$VERIFY_DIR/SHA256SUMS.asc" ||
        abort_verification "Could not download the SHA256SUMS signature."
    curl -fsSL "$RELEASE_URL/topo-release-public.asc" -o "$VERIFY_DIR/key.asc" ||
        abort_verification "Could not download the Topo release public key."
    mkdir -p "$VERIFY_DIR/gnupg"
    chmod 700 "$VERIFY_DIR/gnupg"
    gpg --batch --homedir "$VERIFY_DIR/gnupg" --import "$VERIFY_DIR/key.asc" >/dev/null 2>&1 ||
        abort_verification "Could not import the Topo release public key."
    # The signing key must be the pinned one; gpg's exit code alone would accept
    # any key the attacker shipped alongside a re-signed manifest.
    gpg --batch --homedir "$VERIFY_DIR/gnupg" --status-fd=1 \
        --verify "$VERIFY_DIR/SHA256SUMS.asc" "$VERIFY_DIR/SHA256SUMS" 2>/dev/null |
        awk -v fpr="$TOPO_KEY_FPR" '
            $2 == "VALIDSIG" && (toupper($3) == fpr || toupper($NF) == fpr) { found = 1 }
            END { exit(found ? 0 : 1) }
        ' || abort_verification "SHA256SUMS signature is not from the pinned Topo release key."
    SUMS_FILE="$VERIFY_DIR/SHA256SUMS"
    if [ "$MINIMAL" = false ]; then
        echo -e "  ${GREEN}✓ release manifest signature verified${NC}"
    fi
}

# Verifies $1 against the manifest entry named $2. A missing entry aborts
# (fail-closed) instead of silently skipping the check.
verify_release_file() {
    local file_path="$1"
    local entry_name="$2"
    require_release_manifest
    local expected_sha
    expected_sha=$(awk -v n="$entry_name" '$2 == n || $2 == "*" n {print $1; exit}' "$SUMS_FILE")
    if [ -z "$expected_sha" ]; then
        rm -f "$file_path"
        abort_verification "No SHA256SUMS entry for ${entry_name}."
    fi
    local actual_sha
    actual_sha=$(sha256sum "$file_path" 2>/dev/null | awk '{print $1}')
    if [ "$actual_sha" != "$expected_sha" ]; then
        rm -f "$file_path"
        abort_verification "SHA256 checksum mismatch for ${entry_name}."
    fi
}

# 2. Define paths
INSTALL_DIR="$HOME/.topo"
FINAL_INSTALL="$INSTALL_DIR"
WAS_INSTALLED=false
if [ -e "$INSTALL_DIR" ]; then WAS_INSTALLED=true; fi
STAGED_INSTALL=""
BACKUP_INSTALL=""

cleanup_install_staging() {
    if [ -n "$STAGED_INSTALL" ] && [ -d "$STAGED_INSTALL" ]; then rm -rf "$STAGED_INSTALL"; fi
    if [ -n "$BACKUP_INSTALL" ] && [ -d "$BACKUP_INSTALL" ]; then
        if [ -e "$FINAL_INSTALL" ]; then rm -rf "$FINAL_INSTALL"; fi
        mv "$BACKUP_INSTALL" "$FINAL_INSTALL"
    fi
}
trap 'cleanup_install_staging; cleanup_verify_dir' EXIT

# 3. Clone or download source
if [ "$MINIMAL" = false ]; then
    echo -e "\n${CYAN}☉ Fetching Topo...${NC}"
fi

if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↓ Downloading signed source archive (${TARGET_REF})...${NC}"; fi
require_release_manifest
SRC_ARCHIVE="$VERIFY_DIR/topo-src.tar.gz"
if [ "$TARGET_REF" = "main" ]; then
    abort_verification "The moving main branch has no signed source archive; install a release tag."
fi
curl -fsSL "$RELEASE_URL/topo-src.tar.gz" -o "$SRC_ARCHIVE" ||
    abort_verification "Could not download the signed Topo source archive."
verify_release_file "$SRC_ARCHIVE" "topo-src.tar.gz"
STAGED_INSTALL=$(mktemp -d "$HOME/.topo.install.XXXXXX")
tar -xzC "$STAGED_INSTALL" --strip-components=1 -f "$SRC_ARCHIVE"
touch "$STAGED_INSTALL/.non_git_install"
INSTALL_DIR="$STAGED_INSTALL"
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓ source archive signature and checksum verified${NC}"; fi

# 4. Clean up and provision binaries
if [ "$MINIMAL" = false ]; then
    echo -e "  ${GRAY}🧹 Refining installation directory...${NC}"
fi
cd "$INSTALL_DIR"
printf 'script\n' > .topo-install-source

ARCH=$(uname -m)
BIN_DIR="src/core/bin"

# Ensure binary directory exists
mkdir -p "$BIN_DIR"

# Fetch and verify one engine binary. Any failure aborts: leaving a truncated
# or unverified file behind and then chmod +x'ing it is exactly what M-5 warned
# about, since Python invokes this binary as a subprocess afterwards.
fetch_engine_binary() {
    local bin_name="$1"
    # Stage inside VERIFY_DIR (removed by the EXIT trap) so an aborted install
    # never leaves an unverified file in BIN_DIR — the next run would otherwise
    # mistake that residue for a bundled engine and chmod +x it.
    require_release_manifest
    local staged="$VERIFY_DIR/$bin_name"
    curl -fsSL "$RELEASE_URL/$bin_name" -o "$staged" ||
        abort_verification "Could not download the ${bin_name} engine."
    verify_release_file "$staged" "$bin_name"
    chmod +x "$staged"
    mv -f "$staged" "$BIN_DIR/$bin_name"
    if [ "$MINIMAL" = false ]; then
        echo -e "  ${GREEN}✓ ${bin_name} checksum verified${NC}"
    fi
}

if [[ "$ARCH" == "x86_64" ]]; then
    if [ ! -f "$BIN_DIR/topo-core-x86_64" ]; then
        if [ "$MINIMAL" = false ]; then echo -e "  ${GRAY}↓ Fetching x86_64 engine from ${TARGET_REF}...${NC}"; fi
        fetch_engine_binary "topo-core-x86_64"
    else
        if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}Using bundled x86_64 engine.${NC}"; fi
        chmod +x "$BIN_DIR/topo-core-x86_64" 2>/dev/null || true
    fi
    rm -f "$BIN_DIR/topo-core-aarch64"
elif [[ "$ARCH" == "aarch64" ]] || [[ "$ARCH" == "arm64" ]]; then
    if [ ! -f "$BIN_DIR/topo-core-aarch64" ]; then
        if [ "$MINIMAL" = false ]; then echo -e "  ${YELLOW}↓ ARM64 detected. Fetching optimized engine from ${TARGET_REF}...${NC}"; fi
        fetch_engine_binary "topo-core-aarch64"
    else
        if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}Using bundled ARM64 engine.${NC}"; fi
        chmod +x "$BIN_DIR/topo-core-aarch64" 2>/dev/null || true
    fi
    rm -f "$BIN_DIR/topo-core-x86_64"
fi

# Keep LICENSE for compliance, but remove everything else non-essential
rm -f assets/*.png assets/*.asc 2>/dev/null || true
rm -rf \
    .github/ \
    docs/ \
    tests/ \
    daily_report.md \
    pytest.ini \
    topo.py \
    .gitignore \
    README.md \
    README.zh-CN.md \
    check.sh \
    pyproject.toml \
    packaging/ \
    install.sh \
    topo-core/

# 5. Run the linking script
if [ -e "$FINAL_INSTALL" ]; then
    BACKUP_INSTALL="$HOME/.topo.backup.$$"
    mv "$FINAL_INSTALL" "$BACKUP_INSTALL"
fi
if ! mv "$STAGED_INSTALL" "$FINAL_INSTALL"; then
    if [ -n "$BACKUP_INSTALL" ] && [ -d "$BACKUP_INSTALL" ]; then mv "$BACKUP_INSTALL" "$FINAL_INSTALL"; fi
    abort_verification "Could not activate the verified installation."
fi
STAGED_INSTALL=""
INSTALL_DIR="$FINAL_INSTALL"
cd "$INSTALL_DIR"
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

# Activation is only committed after the new executable has linked correctly.
if [ -n "$BACKUP_INSTALL" ] && [ -d "$BACKUP_INSTALL" ]; then
    rm -rf "$BACKUP_INSTALL"
    BACKUP_INSTALL=""
fi

# OOTB PATH Fix: Offer immediate access via /usr/local/bin if not in PATH
if [ "$MINIMAL" = false ] && ! command -v topo >/dev/null 2>&1; then
    # Use /dev/tty to allow reading input even when piped from curl
    if [ -c /dev/tty ]; then
        echo -e "\n  ${YELLOW}⚠ 'topo' is not yet in your PATH.${NC}"
        echo -e "  ${CYAN}Would you like to link it to ${BOLD}/usr/local/bin${NC}${CYAN} for immediate access? (requires sudo)${NC}"
        printf "  %b[y/N]%b " "${BOLD}" "${NC}"
        read -r choice < /dev/tty || choice="n"
        if [[ "$choice" =~ ^[Yy]$ ]]; then
            if sudo ln -sf "${INSTALL_DIR}/topo" /usr/local/bin/topo; then
                echo -e "  ${GREEN}✓ Linked system-wide. You can now run 'topo' immediately!${NC}"
            fi
        fi
    fi
fi

if ! command -v topo >/dev/null 2>&1; then
    echo -e "  ${YELLOW}⚠ Warning: 'topo' is still not available in PATH.${NC}"
    echo -e "  ${GRAY}You can run it directly with:${NC} ${BOLD}${INSTALL_DIR}/topo${NC}"
    echo -e "  ${GRAY}Or manually link it: ${NC}${BOLD}sudo ln -sf ${INSTALL_DIR}/topo /usr/local/bin/topo${NC}"
fi

# 6. Display final banner and version
if [ "$MINIMAL" = false ]; then
    # Extract version
    TOPO_VER="unknown"
    if [ -f "VERSION" ]; then
        TOPO_VER=$(cat VERSION)
    fi

    echo -e "${CYAN}"
    echo "  ⠶⣶⠶  ⢰⠶⡆ ⢰⠶⡆ ⢰⠶⡆ "
    echo "   ⠿   ⠸⠤⠇ ⢸⠉⠁ ⠸⠤⠇ "
    echo -e "${NC}"
    echo -e " ${CYAN}●${NC} ${BOLD}Topo v${TOPO_VER}${NC} ${GRAY}is digging deeper 🦡${NC}\n"
    
    echo -e "${GRAY}Type '${NC}topo${GRAY}' to start the interactive TUI, or '${NC}topo --help${GRAY}' to explore all commands.${NC}"
    echo -e "${GRAY}If your shell still tries an old '${NC}/usr/bin/topo${GRAY}' path, run '${NC}hash -r${GRAY}' or reopen the terminal.${NC}"
fi
