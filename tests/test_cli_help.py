import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def run_topo_help(*args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "topo"), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    return result.stdout


def test_main_help_documents_whitelist_usage():
    output = run_topo_help("--help")

    assert "Quick Start:" in output
    assert "topo doctor              Diagnose Topo installation and runtime tools" in output
    assert "Whitelist:" in output
    assert "An empty whitelist is normal before you add a path." in output
    assert "Run topo whitelist --help for whitelist details." in output


def test_whitelist_help_explains_manual_rules():
    output = run_topo_help("whitelist", "--help")

    assert "Show manual protection rules." in output
    assert "Manual rules are stored in ~/.config/topo/whitelist.json." in output
    assert "not shown by whitelist list" in output
