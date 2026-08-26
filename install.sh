#!/usr/bin/env bash

set -e

# ANSI High-Contrast Professional Palette (Matching Topo Core)
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    PURPLE='\033[1;95m'
    CYAN='\033[1;36m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    EARTH="$YELLOW"
    RED='\033[1;31m'
    GRAY='\033[38;5;244m'
    BOLD='\033[1m'
    NC='\033[0m' # No Color
else
    PURPLE=''
    CYAN=''
    GREEN=''
    YELLOW=''
    EARTH=''
    RED=''
    GRAY=''
    BOLD=''
    NC=''
fi

start_action() {
    if [ "$MINIMAL" = false ] && [ -t 1 ]; then
        printf "  ${GRAY}%s %s...${NC}" "$1" "$2"
    fi
}

end_action() {
    if [ "$MINIMAL" = false ] && [ -t 1 ]; then
        printf "\r\033[K"
    fi
}

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
    echo -e "${PURPLE}☉ Checking prerequisites...${NC}"
fi

if command -v git >/dev/null 2>&1; then
    if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}git installed${NC}"; fi
else
    if [ "$MINIMAL" = false ]; then echo -e "  ${YELLOW}ℹ${NC} ${GRAY}git not found (signed release installation is unaffected)${NC}"; fi
fi

command -v curl >/dev/null 2>&1 || { echo -e "  ${RED}✗ Error: curl is required but not installed.${NC}"; exit 1; }
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}curl installed${NC}"; fi

command -v python3 >/dev/null 2>&1 || { echo -e "  ${RED}✗ Error: python3 is required but not installed.${NC}"; exit 1; }
# Version, not just presence, and one interpreter call for both halves of the
# answer. The code requires 3.10+ (see the same floor in the `topo` launcher), so
# on Debian 11 or RHEL 8 this script used to tick every box, print its success
# banner, and leave behind a Topo that dies on first run.
if ! PY_VERSION=$(python3 -c 'import sys; print(".".join(str(p) for p in sys.version_info[:3])); sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null); then
    echo -e "  ${RED}✗ Error: Topo requires Python 3.10 or newer (found ${PY_VERSION:-an unknown version}).${NC}"
    echo -e "  ${GRAY}Install a newer python3 and make sure it is the one on your PATH.${NC}"
    exit 1
fi
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}python3 ${PY_VERSION} installed${NC}"; fi
if ! python3 -c "import packaging" >/dev/null 2>&1; then
    echo -e "  ${RED}✗ Error: Python package 'packaging' is required but not installed.${NC}"
    echo -e "  ${GRAY}Install it with one of:${NC}"
    echo -e "    ${BOLD}sudo apt install python3-packaging${NC}        ${GRAY}# Debian/Ubuntu${NC}"
    echo -e "    ${BOLD}sudo dnf install python3-packaging${NC}        ${GRAY}# Fedora/RHEL${NC}"
    echo -e "    ${BOLD}sudo pacman -S python-packaging${NC}           ${GRAY}# Arch/Manjaro${NC}"
    exit 1
fi
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}python packaging installed${NC}"; fi

if [ "$MINIMAL" = false ]; then
    PACKAGE_REMOVE_COMMAND=""
    PACKAGE_VERSION=""
    PACKAGE_FORMAT="system package"
    PACKAGE_REMOVE_METHOD="system package manager"
    # Warn when another package-managed Topo installation is still present.
    # A user-space install may take precedence in PATH, but the system copy
    # remains a second installation source and can cause version confusion.
    if command -v rpm >/dev/null 2>&1 && rpm -q topo >/dev/null 2>&1 && \
        rpm -ql topo 2>/dev/null | grep -Fxq '/usr/bin/topo'; then
        PACKAGE_VERSION="$(rpm -q --qf '%{VERSION}' topo 2>/dev/null || true)"
        PACKAGE_FORMAT="RPM system package"
        if command -v zypper >/dev/null 2>&1; then
            PACKAGE_REMOVE_METHOD="Zypper"
            PACKAGE_REMOVE_COMMAND="sudo zypper remove topo"
        elif command -v dnf >/dev/null 2>&1; then
            PACKAGE_REMOVE_METHOD="DNF"
            PACKAGE_REMOVE_COMMAND="sudo dnf remove topo"
        elif command -v yum >/dev/null 2>&1; then
            PACKAGE_REMOVE_METHOD="YUM"
            PACKAGE_REMOVE_COMMAND="sudo yum remove topo"
        else
            PACKAGE_REMOVE_METHOD="RPM"
            PACKAGE_REMOVE_COMMAND="sudo rpm -e topo"
        fi
    elif command -v dpkg-query >/dev/null 2>&1 && \
        dpkg-query -W -f='${Status}' topo 2>/dev/null | grep -q "install ok installed" && \
        dpkg-query -L topo 2>/dev/null | grep -Fxq '/usr/bin/topo'; then
        PACKAGE_VERSION="$(dpkg-query -W -f='${Version}' topo 2>/dev/null | sed 's/-[^-]*$//' || true)"
        PACKAGE_FORMAT="DEB system package"
        if command -v apt >/dev/null 2>&1; then
            PACKAGE_REMOVE_METHOD="APT"
            PACKAGE_REMOVE_COMMAND="sudo apt remove topo"
        else
            PACKAGE_REMOVE_METHOD="APT"
            PACKAGE_REMOVE_COMMAND="sudo apt-get remove topo"
        fi
    fi

    if [ -n "$PACKAGE_REMOVE_COMMAND" ]; then
        if [ -n "$PACKAGE_VERSION" ]; then
            echo -e "  ${YELLOW}⚠ Topo ${PACKAGE_VERSION} is still installed as an ${PACKAGE_FORMAT}.${NC}"
        else
            echo -e "  ${YELLOW}⚠ Topo is still installed as an ${PACKAGE_FORMAT}.${NC}"
        fi
        echo -e "  ${GRAY}The 'curl | bash' method installs a separate user copy under ~/.topo.${NC}"
        echo -e "  ${GRAY}Keeping both installation methods may cause version or command-path confusion.${NC}"
        echo -e "  ${GRAY}Remove the system package with ${PACKAGE_REMOVE_METHOD}:${NC} ${BOLD}${PACKAGE_REMOVE_COMMAND}${NC}"
        echo -e "  ${GRAY}Then refresh your shell command cache with:${NC} ${BOLD}hash -r${NC}"
    fi
fi

if [ -z "$TARGET_REF" ]; then
    start_action "↺" "Resolving latest stable release"
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
    end_action
    if [ -z "$TARGET_REF" ] || [ "$TARGET_REF" = "latest" ]; then
        echo -e "  ${RED}✗ Error: failed to resolve the latest Topo release.${NC}"
        echo -e "  ${GRAY}Install a specific version with:${NC} ${BOLD}bash install.sh --version v0.6.0${NC}"
        echo -e "  ${GRAY}Install the development branch with:${NC} ${BOLD}bash install.sh --ref main${NC}"
        exit 1
    fi
fi
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}target release ${TARGET_REF}${NC}"; fi

# The launcher directory `topo link` will use, resolved the way
# src/core/paths.py::get_link_target_dir() resolves it: TOPO_LINK_DIR, else
# /usr/local/bin for root, else ~/.local/bin. Deliberately reimplemented in shell
# instead of imported from the tree being installed -- this script comes from
# main, but the tree it installs is whichever release was requested, so an import
# binds install.sh to *that* release's Python API. Importing get_link_target_dir
# did exactly that and broke every install of a release predating it
# (ImportError, then "Could not resolve the launcher path"); the private
# _get_link_target_dir it replaced only worked by accident of still existing.
# The answer may be relative, exactly as Python's is; only python3's stdlib
# expanduser() is borrowed, because ~user has no safe shell equivalent.
# tests/test_install.py runs this function and diffs it against
# get_link_target_dir() on every branch, root included.
resolve_link_target_dir() {
    if [ -n "${TOPO_LINK_DIR:-}" ]; then
        python3 -c 'import os; from pathlib import Path; print(Path(os.environ["TOPO_LINK_DIR"]).expanduser())' </dev/null 2>/dev/null || return 1
        return 0
    fi
    if [ "$(id -u)" -eq 0 ]; then
        printf '%s\n' "/usr/local/bin"
        return 0
    fi
    printf '%s\n' "$HOME/.local/bin"
}

# Where that directory actually lands: `topo link` runs with the install tree as
# its working directory, so a relative TOPO_LINK_DIR resolves under ~/.topo.
absolute_link_dir() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s\n' "$HOME/.topo/$1" ;;
    esac
}

# The engine built for an architecture, or nothing when none is. One answer for
# both callers -- the completeness check below and the provisioning in step 4 --
# because they used to spell the same case statement twice and disagreeing about
# which machines have an engine is precisely the bug that produces an install
# that re-downloads itself forever. src/core/engine.py keeps the same table for
# the Python side, and tests/test_engine.py runs this function against it.
engine_for_arch() {
    case "$1" in
        x86_64) printf '%s\n' "topo-core-x86_64" ;;
        aarch64|arm64) printf '%s\n' "topo-core-aarch64" ;;
    esac
}

# A repeated curl install should be cheap when the requested release is already
# complete. Only skip the verified download when every runtime artifact and
# the launcher/PATH setup match this release; missing, non-executable, or
# stale pieces fall through to the normal atomic upgrade path below. This
# lightweight check does not validate binary checksums.
installed_version_matches() {
    local target_version="${TARGET_REF#v}"
    local local_version
    local link_dir
    local path_entry
    local launcher_path
    local engine_name
    local export_line
    local config_path
    local found_config=false

    [ "$TARGET_REF" != "main" ] || return 1
    [ -f "$HOME/.topo/VERSION" ] || return 1
    local_version=$(tr -d '[:space:]' < "$HOME/.topo/VERSION" 2>/dev/null || true)
    [ "$local_version" = "$target_version" ] || return 1
    [ -x "$HOME/.topo/topo" ] || return 1
    [ -f "$HOME/.topo/src/main.py" ] || return 1

    path_entry=$(resolve_link_target_dir) || return 1
    link_dir=$(absolute_link_dir "$path_entry")
    launcher_path="$link_dir/topo"
    [ -L "$launcher_path" ] || return 1
    [ "$(readlink -f "$launcher_path" 2>/dev/null || true)" = "$(readlink -f "$HOME/.topo/topo" 2>/dev/null || true)" ] || return 1

    # An architecture without an engine has nothing to check here: step 4
    # deliberately installs none. This used to `return 1`, which made every
    # single run of this script re-download and re-install the whole release.
    engine_name=$(engine_for_arch "$(uname -m)")
    if [ -n "$engine_name" ]; then
        [ -x "$HOME/.topo/src/core/bin/$engine_name" ] || return 1
    fi

    if [[ ":${PATH}:" == *":${path_entry}:"* ]]; then
        return 0
    fi
    if [ "$path_entry" = "$HOME/.local/bin" ]; then
        export_line="export PATH=\"\$HOME/.local/bin:\$PATH\""
    else
        export_line="export PATH=\"${path_entry}:\$PATH\""
    fi
    for config_path in "$HOME/.bashrc" "$HOME/.zshrc"; do
        [ -e "$config_path" ] || continue
        found_config=true
        grep -Fq "$export_line" "$config_path" 2>/dev/null || return 1
    done
    [ "$found_config" = true ]
}

if installed_version_matches; then
    if [ "$MINIMAL" = false ]; then
        echo -e "  ${GREEN}✓${NC} ${GRAY}Topo ${BOLD}v${TARGET_REF#v}${NC}${GRAY} is already installed; skipping download.${NC}"
    fi
    exit 0
fi

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
    end_action
    echo -e "  ${RED}✗ ${1}${NC}" >&2
    echo -e "  ${GRAY}Installation was not committed; temporary files were cleaned up and any previous installation and shell configuration were restored.${NC}" >&2
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
    start_action "↓" "Downloading and verifying GPG release manifest"
    curl -fsSL --connect-timeout 10 --retry 3 --retry-delay 2 --retry-connrefused "$RELEASE_URL/SHA256SUMS" -o "$VERIFY_DIR/SHA256SUMS" ||
        abort_verification "Could not download the SHA256SUMS manifest."
    curl -fsSL --connect-timeout 10 --retry 3 --retry-delay 2 --retry-connrefused "$RELEASE_URL/SHA256SUMS.asc" -o "$VERIFY_DIR/SHA256SUMS.asc" ||
        abort_verification "Could not download the SHA256SUMS signature."
    curl -fsSL --connect-timeout 10 --retry 3 --retry-delay 2 --retry-connrefused "$RELEASE_URL/topo-release-public.asc" -o "$VERIFY_DIR/key.asc" ||
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
    end_action
    if [ "$MINIMAL" = false ]; then
        echo -e "  ${GREEN}✓${NC} ${GRAY}release manifest signature verified${NC}"
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
INSTALL_ACTIVATED=false
LAUNCHER_PATH=""
SHELL_CONFIG_SNAPSHOT_DIR=""

snapshot_shell_configs() {
    SHELL_CONFIG_SNAPSHOT_DIR=$(mktemp -d "${TMPDIR:-/tmp}/topo-shell-config.XXXXXX")
    for config_name in .bashrc .zshrc; do
        config_path="$HOME/$config_name"
        if [ -e "$config_path" ]; then
            cp -p "$config_path" "$SHELL_CONFIG_SNAPSHOT_DIR/$config_name"
        else
            : > "$SHELL_CONFIG_SNAPSHOT_DIR/$config_name.missing"
        fi
    done
}

restore_shell_configs() {
    if [ -z "$SHELL_CONFIG_SNAPSHOT_DIR" ] || [ ! -d "$SHELL_CONFIG_SNAPSHOT_DIR" ]; then
        return
    fi
    for config_name in .bashrc .zshrc; do
        config_path="$HOME/$config_name"
        if [ -f "$SHELL_CONFIG_SNAPSHOT_DIR/$config_name.missing" ]; then
            rm -f "$config_path"
        elif [ -f "$SHELL_CONFIG_SNAPSHOT_DIR/$config_name" ]; then
            cp -p "$SHELL_CONFIG_SNAPSHOT_DIR/$config_name" "$config_path"
        fi
    done
}

cleanup_install_staging() {
    if [ -n "$STAGED_INSTALL" ] && [ -d "$STAGED_INSTALL" ]; then rm -rf "$STAGED_INSTALL"; fi
    if [ "$INSTALL_ACTIVATED" = true ]; then
        if [ "$WAS_INSTALLED" = false ] && [ -n "$LAUNCHER_PATH" ] && [ -L "$LAUNCHER_PATH" ]; then
            launcher_target=$(readlink -f "$LAUNCHER_PATH" 2>/dev/null || true)
            expected_target=$(readlink -f "$FINAL_INSTALL/topo" 2>/dev/null || true)
            if [ -n "$launcher_target" ] && [ "$launcher_target" = "$expected_target" ]; then
                rm -f "$LAUNCHER_PATH"
            fi
        fi
        if [ -e "$FINAL_INSTALL" ]; then rm -rf "$FINAL_INSTALL"; fi
        restore_shell_configs
    fi
    if [ -n "$BACKUP_INSTALL" ] && [ -d "$BACKUP_INSTALL" ]; then
        mv "$BACKUP_INSTALL" "$FINAL_INSTALL"
    fi
    if [ -n "$SHELL_CONFIG_SNAPSHOT_DIR" ] && [ -d "$SHELL_CONFIG_SNAPSHOT_DIR" ]; then
        rm -rf "$SHELL_CONFIG_SNAPSHOT_DIR"
    fi
}
trap 'cleanup_install_staging; cleanup_verify_dir' EXIT

# 3. Clone or download source
if [ "$MINIMAL" = false ]; then
    echo -e "\n${PURPLE}☉ Fetching Topo...${NC}"
fi

require_release_manifest
SRC_ARCHIVE="$VERIFY_DIR/topo-src.tar.gz"
if [ "$TARGET_REF" = "main" ]; then
    abort_verification "The moving main branch has no signed source archive; install a release tag."
fi
start_action "↓" "Downloading signed source archive (${TARGET_REF})"
curl -fsSL --connect-timeout 10 --retry 3 --retry-delay 2 --retry-connrefused "$RELEASE_URL/topo-src.tar.gz" -o "$SRC_ARCHIVE" ||
    abort_verification "Could not download the signed Topo source archive."
verify_release_file "$SRC_ARCHIVE" "topo-src.tar.gz"
STAGED_INSTALL=$(mktemp -d "$HOME/.topo.install.XXXXXX")
tar -xzC "$STAGED_INSTALL" --strip-components=1 -f "$SRC_ARCHIVE"
touch "$STAGED_INSTALL/.non_git_install"
INSTALL_DIR="$STAGED_INSTALL"
end_action
if [ "$MINIMAL" = false ]; then echo -e "  ${GREEN}✓${NC} ${GRAY}source archive signature and checksum verified${NC}"; fi

# 4. Clean up and provision binaries
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
    # Stage inside VERIFY_DIR (removed by the EXIT trap) so an aborted download
    # never overwrites the bundled engine with a truncated or unverified file.
    require_release_manifest
    local staged="$VERIFY_DIR/$bin_name"
    start_action "↓" "Fetching ${bin_name} engine"
    curl -fsSL --connect-timeout 10 --retry 3 --retry-delay 2 --retry-connrefused "$RELEASE_URL/$bin_name" -o "$staged" ||
        abort_verification "Could not download the ${bin_name} engine."
    verify_release_file "$staged" "$bin_name"
    chmod +x "$staged"
    mv -f "$staged" "$BIN_DIR/$bin_name"
    end_action
    if [ "$MINIMAL" = false ]; then
        echo -e "  ${GREEN}✓${NC} ${GRAY}${bin_name} downloaded, verified, and installed${NC}"
    fi
}

# Exactly one engine survives this, and on an architecture nobody builds for,
# none does. The source archive carries both, and leaving the wrong one behind
# hands Topo a binary the kernel refuses to exec -- src/core/engine.py reaches
# the same conclusion from the same table and falls back to pure Python.
ENGINE_NAME=$(engine_for_arch "$ARCH")
rm -f "$BIN_DIR/topo-core-x86_64" "$BIN_DIR/topo-core-aarch64"
if [ -n "$ENGINE_NAME" ]; then
    fetch_engine_binary "$ENGINE_NAME"
elif [ "$MINIMAL" = false ]; then
    echo -e "  ${YELLOW}ℹ${NC} ${GRAY}No prebuilt engine for ${ARCH}; scanning will use the slower pure-Python path.${NC}"
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
    requirements-dev.txt \
    packaging/ \
    install.sh \
    topo-core/ \
    tach.toml

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
INSTALL_ACTIVATED=true
INSTALL_DIR="$FINAL_INSTALL"
cd "$INSTALL_DIR"
if [ "$MINIMAL" = false ]; then
    echo -e "\n${PURPLE}☉ Configuring system...${NC}"
fi
chmod +x topo
snapshot_shell_configs

# Resolve the launcher path before creating the link so failure cleanup always
# knows which first-install launcher belongs to us. resolve_link_target_dir()
# mirrors run_install_link()'s own choice of directory; see its comment for why
# this is shell and not an import of the tree we just unpacked.
LINK_TARGET_DIR=$(resolve_link_target_dir) ||
    abort_verification "Could not resolve the launcher path before running topo link."
LAUNCHER_PATH="$(absolute_link_dir "$LINK_TARGET_DIR")/topo"

# Updates and minimal installs suppress presentation while still performing the
# PATH repair inside run_install_link().
if [ "$WAS_INSTALLED" = true ] || [ "$MINIMAL" = true ]; then
    ./topo link --silent || abort_verification "Failed to create or update launcher symbolic link."
else
    ./topo link || abort_verification "Failed to create launcher symbolic link."
fi

# Verify the link points to this transaction's activated launcher rather than
# merely checking that a file exists.
if [ ! -L "$LAUNCHER_PATH" ] || [ "$(readlink -f "$LAUNCHER_PATH" 2>/dev/null || true)" != "$(readlink -f "$FINAL_INSTALL/topo" 2>/dev/null || true)" ]; then
    abort_verification "Symbolic link verification failed after running topo link."
fi

if [ "$MINIMAL" = false ] && [ "$WAS_INSTALLED" = true ]; then
    DISP_LAUNCHER="${LAUNCHER_PATH/#"$HOME"/~}"
    echo -e "  ${GREEN}✓${NC} ${GRAY}Executable linked to ${BOLD}${DISP_LAUNCHER}${NC}"
fi

# Activation is only committed after the new executable has linked correctly.
if [ -n "$BACKUP_INSTALL" ] && [ -d "$BACKUP_INSTALL" ]; then
    rm -rf "$BACKUP_INSTALL"
    BACKUP_INSTALL=""
fi
INSTALL_ACTIVATED=false
rm -rf "$SHELL_CONFIG_SNAPSHOT_DIR"
SHELL_CONFIG_SNAPSHOT_DIR=""

# OOTB PATH Fix: Offer immediate access via /usr/local/bin if not in PATH
if [ "$MINIMAL" = false ] && ! command -v topo >/dev/null 2>&1; then
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
    TOPO_VER="unknown"
    if [ -f "VERSION" ]; then
        TOPO_VER=$(cat VERSION)
    fi

    if [ "$WAS_INSTALLED" = true ]; then
        echo -e "  ${GREEN}✓${NC} ${GRAY}Topo updated to ${BOLD}v${TOPO_VER}${NC}${GRAY} successfully!${NC}"
    else
        echo -e "  ${GREEN}✓${NC} ${GRAY}Topo ${BOLD}v${TOPO_VER}${NC}${GRAY} installed successfully!${NC}"
    fi

    echo -e "\n${EARTH} ⠶⣶⠶  ⢰⠶⡆ ⢰⠶⡆ ⢰⠶⡆ ${NC}"
    echo -e "${EARTH}  ⠿   ⠸⠤⠇ ⢸⠉⠁ ⠸⠤⠇ ${NC}  ${PURPLE}●${NC} ${GRAY}v${TOPO_VER} is digging deeper 🦡${NC}\n"
    
    echo -e "${GRAY}Type '${NC}${BOLD}topo${NC}${GRAY}' to get started, or '${NC}${BOLD}topo --help${NC}${GRAY}' to explore all commands.${NC}"
    if [ -n "$PACKAGE_REMOVE_COMMAND" ]; then
        echo -e "${YELLOW}⚠ A system package version may still be selected by this shell.${NC}"
        echo -e "${GRAY}If '${NC}${BOLD}topo --version${NC}${GRAY}' shows the older version, run '${NC}${BOLD}hash -r${NC}${GRAY}' or open a new terminal.${NC}"
    else
        echo -e "${GRAY}If your shell still uses an old command path, run '${NC}${BOLD}hash -r${NC}${GRAY}' or reopen the terminal.${NC}"
    fi
fi
