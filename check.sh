#!/bin/bash
# Exit immediately if a command exits with a non-zero status
set -e

# ANSI color codes for pretty output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

AUTO_FIX=0
if [ "$1" == "--fix" ]; then
    AUTO_FIX=1
    echo -e "${CYAN}🛠️  Auto-fix mode enabled! Tools will attempt to fix issues automatically.${NC}\n"
else
    echo -e "${BLUE}▶ Starting Local Pre-commit Checks for Topo...${NC}"
    echo -e "${GRAY}💡 Hint: Run './check.sh --fix' to automatically fix linting errors.${NC}\n"
fi

echo -e "${YELLOW}🧹 1. Formatting Code...${NC}"
# Formatters are always safe to run
ruff format src tests
cargo fmt --manifest-path topo-core/Cargo.toml
echo -e "${GREEN}✓ Formatting complete.${NC}\n"

echo -e "${YELLOW}🔍 2. Running Python Linters...${NC}"
if [ $AUTO_FIX -eq 1 ]; then
    ruff check --fix src tests
else
    ruff check src tests
fi
mypy src/
echo -e "${GREEN}✓ Python linting complete.${NC}\n"

echo -e "${YELLOW}🦀 3. Running Rust Clippy...${NC}"
if [ $AUTO_FIX -eq 1 ]; then
    cargo clippy --manifest-path topo-core/Cargo.toml --fix --allow-dirty --allow-no-vcs -- -D warnings
else
    cargo clippy --manifest-path topo-core/Cargo.toml -- -D warnings
fi
echo -e "${GREEN}✓ Rust linting complete.${NC}\n"

echo -e "${YELLOW}🐚 4. Running ShellCheck...${NC}"
find . -type f -name '*.sh' -exec shellcheck {} +
echo -e "${GREEN}✓ Shell script linting complete.${NC}\n"

echo -e "${YELLOW}🧪 5. Running Tests...${NC}"
pytest -q
cargo test --manifest-path topo-core/Cargo.toml
echo -e "${GREEN}✓ All tests passed.${NC}\n"

echo -e "${GREEN}✅ All checks passed successfully! You are ready to git add & commit.${NC}"
