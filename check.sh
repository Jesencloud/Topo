#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

# ANSI High-Contrast Professional Palette (Matching Topo Core & install.sh)
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
    PURPLE='\033[1;95m'
    GREEN='\033[0;32m'
    YELLOW='\033[1;33m'
    RED='\033[1;31m'
    GRAY='\033[38;5;244m'
    NC='\033[0m' # No Color
else
    PURPLE=''
    GREEN=''
    YELLOW=''
    RED=''
    GRAY=''
    NC=''
fi

AUTO_FIX=0
if [ "$1" == "--fix" ]; then
    AUTO_FIX=1
    echo -e "${PURPLE}🛠️  Auto-fix mode enabled!${NC} ${GRAY}Tools will attempt to fix issues automatically.${NC}\n"
else
    echo -e "${PURPLE}▶ Starting Local Pre-commit Checks for Topo...${NC}"
    echo -e "${GRAY}💡 Hint: Run './check.sh --fix' to automatically fix linting errors.${NC}\n"
fi

# Mandatory configuration check
if [ ! -f ".vulture_whitelist.py" ]; then
    echo -e "${RED}❌ Error: .vulture_whitelist.py is missing! Dead code whitelist file must exist.${NC}"
    exit 1
fi

echo -e "${PURPLE}☉ 1. Formatting Code...${NC}"
if ! OUT=$(ruff format src tests 2>&1); then
    echo -e "${RED}❌ Ruff format failed:${NC}\n$OUT"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} ${GRAY}Ruff format${NC}"

if ! OUT=$(cargo fmt --manifest-path topo-core/Cargo.toml 2>&1); then
    echo -e "${RED}❌ Cargo fmt failed:${NC}\n$OUT"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} ${GRAY}Cargo fmt${NC}"
echo -e "  ${GREEN}✓${NC} ${GRAY}Formatting complete.${NC}\n"

echo -e "${PURPLE}☉ 2. Running Python Linters...${NC}"
if [ $AUTO_FIX -eq 1 ]; then
    RUFF_CMD="ruff check --fix src tests"
else
    RUFF_CMD="ruff check src tests"
fi
if ! OUT=$($RUFF_CMD 2>&1); then
    echo -e "${RED}❌ Ruff check failed:${NC}\n$OUT"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} ${GRAY}Ruff check${NC}"

if ! OUT=$(mypy --check-untyped-defs src/ 2>&1); then
    echo -e "${RED}❌ Mypy type check failed:${NC}\n$OUT"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} ${GRAY}Mypy type check${NC}"

if command -v tach &> /dev/null; then
    if ! OUT=$(tach check 2>&1); then
        echo -e "${RED}❌ Tach module check failed:${NC}\n$OUT"
        exit 1
    fi
    echo -e "  ${GREEN}✓${NC} ${GRAY}Tach module boundary check${NC}"
else
    echo -e "  ${YELLOW}⚠️${NC}  ${GRAY}Tach not installed; module check SKIPPED (pip install tach).${NC}"
fi

if command -v vulture &> /dev/null; then
    if ! OUT=$(vulture src/ .vulture_whitelist.py 2>&1); then
        echo -e "${RED}❌ Vulture dead code check failed:${NC}\n$OUT"
        exit 1
    fi
    echo -e "  ${GREEN}✓${NC} ${GRAY}Vulture dead code check${NC}"
else
    echo -e "  ${YELLOW}⚠️${NC}  ${GRAY}Vulture not installed; dead code check SKIPPED (pip install vulture).${NC}"
fi
echo -e "  ${GREEN}✓${NC} ${GRAY}Python linting complete.${NC}\n"

echo -e "${PURPLE}☉ 3. Running Rust Linters...${NC}"
if [ $AUTO_FIX -eq 1 ]; then
    CLIPPY_CMD="cargo clippy --quiet --manifest-path topo-core/Cargo.toml --fix --allow-dirty --allow-no-vcs -- -D warnings"
else
    CLIPPY_CMD="cargo clippy --quiet --manifest-path topo-core/Cargo.toml -- -D warnings"
fi
if ! OUT=$($CLIPPY_CMD 2>&1); then
    echo -e "${RED}❌ Cargo clippy failed:${NC}\n$OUT"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} ${GRAY}Cargo clippy${NC}"
echo -e "  ${GREEN}✓${NC} ${GRAY}Rust linting complete.${NC}\n"

echo -e "${PURPLE}☉ 4. Synchronizing local Rust engine...${NC}"
ENGINE_ARCH=""
case "$(uname -m)" in
    x86_64)
        ENGINE_ARCH="x86_64"
        ;;
    aarch64|arm64)
        ENGINE_ARCH="aarch64"
        ;;
    *)
        echo -e "  ${YELLOW}⚠${NC} ${GRAY}Unsupported host architecture; local engine sync skipped.${NC}"
        ;;
esac

if [ -n "$ENGINE_ARCH" ]; then
    ENGINE_BIN="src/core/bin/topo-core-${ENGINE_ARCH}"
    ENGINE_SOURCE_CHANGED=0
    if [ ! -x "$ENGINE_BIN" ]; then
        ENGINE_SOURCE_CHANGED=1
    elif [ "topo-core/Cargo.toml" -nt "$ENGINE_BIN" ] || [ "topo-core/Cargo.lock" -nt "$ENGINE_BIN" ] ||
        find topo-core/src -type f -newer "$ENGINE_BIN" -print -quit | grep -q .; then
        ENGINE_SOURCE_CHANGED=1
    fi

    if [ "$ENGINE_SOURCE_CHANGED" -eq 1 ]; then
        echo -e "  ${GRAY}Rust sources are newer than ${ENGINE_BIN}; building release engine...${NC}"
        if ! OUT=$(cargo build --quiet --release --manifest-path topo-core/Cargo.toml 2>&1); then
            echo -e "${RED}❌ Rust engine build failed:${NC}\n$OUT"
            exit 1
        fi
        ENGINE_TMP=$(mktemp "${ENGINE_BIN}.tmp.XXXXXX")
        if ! cp "topo-core/target/release/topo-core" "$ENGINE_TMP"; then
            rm -f "$ENGINE_TMP"
            echo -e "${RED}❌ Could not stage the rebuilt local engine.${NC}"
            exit 1
        fi
        chmod +x "$ENGINE_TMP"
        mv -f "$ENGINE_TMP" "$ENGINE_BIN"
        echo -e "  ${GREEN}✓${NC} ${GRAY}${ENGINE_BIN} rebuilt and updated.${NC}"
    else
        echo -e "  ${GREEN}✓${NC} ${GRAY}${ENGINE_BIN} is up to date.${NC}"
    fi
fi
echo -e "  ${GREEN}✓${NC} ${GRAY}Local Rust engine synchronization complete.${NC}\n"

echo -e "${PURPLE}☉ 5. Running ShellCheck...${NC}"
if ! OUT=$(find . -type f -name '*.sh' -exec shellcheck {} + 2>&1); then
    echo -e "${RED}❌ ShellCheck failed:${NC}\n$OUT"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} ${GRAY}Shell scripts validated${NC}"
echo -e "  ${GREEN}✓${NC} ${GRAY}Shell script linting complete.${NC}\n"

echo -e "${PURPLE}☉ 6. Running Tests...${NC}"
if [ -t 1 ]; then echo -e "  ${GRAY}↓ Running Python application tests (pytest)...${NC}"; fi
if ! OUT=$(pytest -q -p no:tach 2>&1); then
    echo -e "${RED}❌ Python tests failed:${NC}\n$OUT"
    exit 1
fi
if [ -t 1 ]; then printf "\033[1A\033[K"; fi
PY_COUNT=$(echo "$OUT" | grep -oE '[0-9]+ passed' | head -n 1)
echo -e "  ${GREEN}✓${NC} ${GRAY}${PY_COUNT:-all passed} Python application tests (pytest)${NC}"

if [ -t 1 ]; then echo -e "  ${GRAY}↓ Running Rust core engine tests (cargo test)...${NC}"; fi
if ! OUT=$(cargo test --quiet --manifest-path topo-core/Cargo.toml 2>&1); then
    echo -e "${RED}❌ Rust engine tests failed:${NC}\n$OUT"
    exit 1
fi
if [ -t 1 ]; then printf "\033[1A\033[K"; fi
RS_COUNT=$(printf '%s\n' "$OUT" | awk '
    /test result: ok/ {
        for (i = 1; i <= NF; i++) {
            if ($(i + 1) == "passed;") {
                total += $i
                found = 1
            }
        }
    }
    END { if (found) print total " passed" }
')
echo -e "  ${GREEN}✓${NC} ${GRAY}${RS_COUNT:-all passed} Rust core engine tests (cargo test)${NC}"
echo -e "  ${GREEN}✓${NC} ${GRAY}All test suites passed successfully.${NC}\n"

echo -e " ${GREEN}✓${NC} ${GRAY}All checks passed successfully! 🎉${NC}"
