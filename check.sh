#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

# ANSI color codes for pretty output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
GRAY='\033[0;90m'
RED='\033[0;31m'
NC='\033[0m' # No Color

AUTO_FIX=0
if [ "$1" == "--fix" ]; then
    AUTO_FIX=1
    echo -e "${CYAN}🛠️  Auto-fix mode enabled! Tools will attempt to fix issues automatically.${NC}\n"
else
    echo -e "${BLUE}▶ Starting Local Pre-commit Checks for Topo...${NC}"
    echo -e "${GRAY}💡 Hint: Run './check.sh --fix' to automatically fix linting errors.${NC}\n"
fi

# Mandatory configuration check
if [ ! -f ".vulture_whitelist.py" ]; then
    echo -e "${RED}❌ Error: .vulture_whitelist.py is missing! Dead code whitelist file must exist.${NC}"
    exit 1
fi

echo -e "${YELLOW}🧹 1. Formatting Code...${NC}"
if ! OUT=$(ruff format src tests 2>&1); then
    echo -e "${RED}❌ Ruff format failed:${NC}\n$OUT"
    exit 1
fi
echo -e "${GRAY}  ✓ Ruff format${NC}"

if ! OUT=$(cargo fmt --manifest-path topo-core/Cargo.toml 2>&1); then
    echo -e "${RED}❌ Cargo fmt failed:${NC}\n$OUT"
    exit 1
fi
echo -e "${GRAY}  ✓ Cargo fmt${NC}"
echo -e "${GREEN}✓ Formatting complete.${NC}\n"

echo -e "${YELLOW}🔍 2. Running Python Linters...${NC}"
if [ $AUTO_FIX -eq 1 ]; then
    RUFF_CMD="ruff check --fix src tests"
else
    RUFF_CMD="ruff check src tests"
fi
if ! OUT=$($RUFF_CMD 2>&1); then
    echo -e "${RED}❌ Ruff check failed:${NC}\n$OUT"
    exit 1
fi
echo -e "${GRAY}  ✓ Ruff check${NC}"

if ! OUT=$(mypy --check-untyped-defs src/ 2>&1); then
    echo -e "${RED}❌ Mypy type check failed:${NC}\n$OUT"
    exit 1
fi
echo -e "${GRAY}  ✓ Mypy type check${NC}"

if command -v tach &> /dev/null; then
    if ! OUT=$(tach check 2>&1); then
        echo -e "${RED}❌ Tach module check failed:${NC}\n$OUT"
        exit 1
    fi
    echo -e "${GRAY}  ✓ Tach module boundary check${NC}"
else
    echo -e "${YELLOW}  ⚠️  Tach not installed; module check SKIPPED (pip install tach).${NC}"
fi

if command -v vulture &> /dev/null; then
    if ! OUT=$(vulture src/ .vulture_whitelist.py 2>&1); then
        echo -e "${RED}❌ Vulture dead code check failed:${NC}\n$OUT"
        exit 1
    fi
    echo -e "${GRAY}  ✓ Vulture dead code check${NC}"
else
    echo -e "${YELLOW}  ⚠️  Vulture not installed; dead code check SKIPPED (pip install vulture).${NC}"
fi
echo -e "${GREEN}✓ Python linting complete.${NC}\n"

echo -e "${YELLOW}🦀 3. Running Rust Linters...${NC}"
if [ $AUTO_FIX -eq 1 ]; then
    CLIPPY_CMD="cargo clippy --quiet --manifest-path topo-core/Cargo.toml --fix --allow-dirty --allow-no-vcs -- -D warnings"
else
    CLIPPY_CMD="cargo clippy --quiet --manifest-path topo-core/Cargo.toml -- -D warnings"
fi
if ! OUT=$($CLIPPY_CMD 2>&1); then
    echo -e "${RED}❌ Cargo clippy failed:${NC}\n$OUT"
    exit 1
fi
echo -e "${GRAY}  ✓ Cargo clippy${NC}"
echo -e "${GREEN}✓ Rust linting complete.${NC}\n"

echo -e "${YELLOW}🐚 4. Running ShellCheck...${NC}"
if ! OUT=$(find . -type f -name '*.sh' -exec shellcheck {} + 2>&1); then
    echo -e "${RED}❌ ShellCheck failed:${NC}\n$OUT"
    exit 1
fi
echo -e "${GRAY}  ✓ Shell scripts validated${NC}"
echo -e "${GREEN}✓ Shell script linting complete.${NC}\n"

echo -e "${YELLOW}🧪 5. Running Tests...${NC}"
pytest -q -p no:tach
cargo test --quiet --manifest-path topo-core/Cargo.toml
echo -e "${GREEN}✓ All tests passed.${NC}\n"

echo -e "${GREEN}✅ All checks passed successfully! 🎉${NC}"
