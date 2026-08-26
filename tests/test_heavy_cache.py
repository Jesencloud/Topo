from pathlib import Path

import pytest

from src.core.constants import DEV_CACHES
from src.core.heavy_cache import (
    AI_MODEL_CACHE_DEFS,
    CONTAINER_CACHE_DEFS,
    PACKAGE_MANAGER_CACHE_DEFS,
    CachePathDef,
    get_ai_model_cleanup_defs,
    get_analyze_cache_defs,
    get_container_cache_def,
)


def test_analyze_cache_defs_cover_heavy_cache_families(test_env):
    defs = {definition.key: definition for definition in get_analyze_cache_defs()}

    assert {definition.key for definition in PACKAGE_MANAGER_CACHE_DEFS} == {
        "apt",
        "pacman",
        "zypper",
        "dnf",
    }
    assert {definition.key for definition in CONTAINER_CACHE_DEFS} == {
        "docker-user",
        "docker-system",
        "podman-cache",
        "flatpak-data",
        "snapd",
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


def test_snap_packages_reach_the_analyze_root_view():
    """Snap is Ubuntu's largest block outside Home, so Analyze has to show it
    even though Clean, not a generic delete, owns the cleanup."""
    defs = {definition.key: definition for definition in get_analyze_cache_defs()}

    assert defs["snapd"].resolved_path() == Path("/var/lib/snapd")
    assert defs["snapd"].label == "Snap Packages"


def test_fallback_paths_pick_up_a_relocated_cache(tmp_path):
    """dnf5 moved its cache and a snap-packaged Docker keeps its data under
    /var/snap; both are the same family under a different path, so the primary
    path being absent must not turn the row into an empty one."""
    moved = tmp_path / "relocated"
    moved.mkdir()
    definition = CachePathDef(
        key="probe",
        label="Probe",
        path=str(tmp_path / "absent"),
        fallback_paths=(str(tmp_path / "also-absent"), str(moved)),
    )

    assert definition.resolved_path() == moved
    # With nothing to fall back to the primary path is still what gets reported,
    # so the row shows the canonical location rather than disappearing.
    assert CachePathDef(key="p", label="P", path=str(tmp_path / "absent")).resolved_path() == (
        tmp_path / "absent"
    )


def test_snap_packaged_docker_data_is_found_under_var_snap():
    docker_system = get_container_cache_def("docker-system")

    assert docker_system.fallback_paths == ("/var/snap/docker/common/var-lib-docker",)
    assert get_container_cache_def("snapd").fallback_paths == ()


def test_apt_lists_its_binary_indexes_as_extra_paths(tmp_path):
    """`apt-get clean` frees more than archives/, and Clean has to measure it (D4).

    pkgCacheFile::RemoveCaches() unlinks /var/cache/apt/pkgcache.bin and
    srcpkgcache.bin, 44.5 MB each on debian:stable-slim against 8.6 MB of
    archives/ -- apt-get(8) does not mention them. They belong beside the primary
    path rather than instead of it, which is what separates extra_paths from
    fallback_paths.
    """
    apt = {definition.key: definition for definition in PACKAGE_MANAGER_CACHE_DEFS}["apt"]

    assert apt.extra_paths == (
        "/var/cache/apt/pkgcache.bin",
        "/var/cache/apt/srcpkgcache.bin",
    )

    # Analyze shows one path per row, so resolved_path() must not start answering
    # with an extra: the primary is reported even when only an extra exists.
    extra = tmp_path / "pkgcache.bin"
    extra.write_bytes(b"")
    definition = CachePathDef(
        key="probe",
        label="Probe",
        path=str(tmp_path / "absent"),
        extra_paths=(str(extra),),
    )

    assert definition.resolved_path() == tmp_path / "absent"
