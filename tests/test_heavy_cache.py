from pathlib import Path

import pytest

from src.core.constants import DEV_CACHES
from src.core.heavy_cache import (
    AI_MODEL_CACHE_DEFS,
    CONTAINER_CACHE_DEFS,
    PACKAGE_MANAGER_CACHE_DEFS,
    get_ai_model_cleanup_defs,
    get_analyze_cache_defs,
    get_container_cache_def,
    get_package_manager_cleaner,
)


def test_analyze_cache_defs_cover_heavy_cache_families(test_env):
    defs = {definition.key: definition for definition in get_analyze_cache_defs()}

    assert {definition.key for definition in PACKAGE_MANAGER_CACHE_DEFS} == {"apt", "pacman", "dnf"}
    assert {definition.key for definition in CONTAINER_CACHE_DEFS} == {
        "docker-user",
        "docker-system",
        "podman-cache",
        "flatpak-data",
    }
    assert {definition.key for definition in AI_MODEL_CACHE_DEFS} == {
        "ollama-models",
        "huggingface",
        "lm-studio",
        "torch",
        "triton",
        "cuda",
    }
    assert defs["apt"].resolved_path() == Path("/var/cache/apt/archives")
    assert defs["docker-system"].resolved_path() == Path("/var/lib/docker")
    assert defs["huggingface"].resolved_path() == test_env / ".cache/huggingface/hub"
    assert defs["ollama-models"].resolved_path() == test_env / ".ollama/models"


def test_package_manager_cleaners_define_commands():
    apt = get_package_manager_cleaner("ubuntu")
    dnf = get_package_manager_cleaner("fedora")
    pacman = get_package_manager_cleaner("arch")

    assert apt is not None
    assert apt.command == ("apt-get", "clean")
    assert dnf is not None
    assert dnf.command == ("dnf", "clean", "all")
    assert pacman is not None
    assert pacman.command == ("pacman", "-Sc", "--noconfirm")
    assert get_package_manager_cleaner("unknown") is None


def test_container_and_ai_clean_targets_resolve_home_dynamically(test_env):
    assert get_container_cache_def("podman-cache").resolved_path() == test_env / ".cache/containers"
    with pytest.raises(KeyError, match="missing"):
        get_container_cache_def("missing")

    cleanup_defs = {definition.key: definition for definition in get_ai_model_cleanup_defs()}
    assert cleanup_defs["ollama-blobs"].resolved_path() == test_env / ".ollama/models/blobs"
    assert cleanup_defs["ollama-blobs"].age_days == 14
    assert cleanup_defs["cuda"].resolved_path() == test_env / ".nv/ComputeCache"
    assert cleanup_defs["cuda"].age_days == 7


def test_dev_caches_do_not_duplicate_ai_model_definitions():
    assert set(DEV_CACHES) == {"npm", "pip", "cargo", "go"}
