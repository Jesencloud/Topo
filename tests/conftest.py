import os
import shutil
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def test_env():
    """Create a temporary home directory for testing to prevent accidental deletion."""
    old_home = os.environ.get("HOME")
    temp_home = tempfile.mkdtemp(prefix="topo_test_home_")
    os.environ["HOME"] = temp_home

    # Pre-create some common structure
    Path(temp_home).joinpath(".config").mkdir(parents=True)
    Path(temp_home).joinpath(".cache").mkdir(parents=True)
    Path(temp_home).joinpath(".local/share").mkdir(parents=True)

    yield Path(temp_home)

    # Cleanup
    shutil.rmtree(temp_home)
    if old_home:
        os.environ["HOME"] = old_home
    else:
        del os.environ["HOME"]
