"""Shared metadata for heavyweight cache families.

Analyze uses these definitions to surface large cache roots. Clean owns the
actual cleanup actions because package managers, containers, and model tools
usually need command-specific behavior rather than direct path deletion.
"""

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CachePathDef:
    key: str
    label: str
    path: str
    min_display_bytes: int = 10 * 1024 * 1024

    def resolved_path(self) -> Path:
        return Path(self.path).expanduser()


@dataclass(frozen=True)
class AgeCleanupDef:
    key: str
    label: str
    path: str
    age_days: int

    def resolved_path(self) -> Path:
        return Path(self.path).expanduser()


@dataclass(frozen=True)
class PackageManagerCleanerDef:
    key: str
    label: str
    os_ids: tuple[str, ...]
    executable: str
    command: tuple[str, ...]


PACKAGE_MANAGER_CACHE_DEFS = (
    CachePathDef(
        key="apt",
        label="Apt Cache",
        path="/var/cache/apt/archives",
    ),
    CachePathDef(
        key="pacman",
        label="Pacman Cache",
        path="/var/cache/pacman/pkg",
    ),
    CachePathDef(
        key="dnf",
        label="Dnf Cache",
        path="/var/cache/dnf",
    ),
)

PACKAGE_MANAGER_CLEANER_DEFS = (
    PackageManagerCleanerDef(
        key="dnf",
        label="DNF cache",
        os_ids=("fedora", "rhel", "centos"),
        executable="dnf",
        command=("dnf", "clean", "all"),
    ),
    PackageManagerCleanerDef(
        key="apt",
        label="APT cache",
        os_ids=("ubuntu", "debian"),
        executable="apt-get",
        command=("apt-get", "clean"),
    ),
    PackageManagerCleanerDef(
        key="pacman",
        label="Pacman cache",
        os_ids=("arch",),
        executable="pacman",
        command=("pacman", "-Sc", "--noconfirm"),
    ),
)

CONTAINER_CACHE_DEFS = (
    CachePathDef(
        key="docker-user",
        label="Docker Data",
        path="~/.docker",
    ),
    CachePathDef(
        key="docker-system",
        label="Docker System",
        path="/var/lib/docker",
    ),
    CachePathDef(
        key="podman-cache",
        label="Podman Transfer Cache",
        path="~/.cache/containers",
    ),
    CachePathDef(
        key="flatpak-data",
        label="Flatpak Data",
        path="~/.local/share/flatpak",
    ),
)

AI_MODEL_CACHE_DEFS = (
    CachePathDef(
        key="ollama-models",
        label="Ollama Models",
        path="~/.ollama/models",
    ),
    CachePathDef(
        key="huggingface",
        label="HuggingFace Hub",
        path="~/.cache/huggingface/hub",
    ),
    CachePathDef(
        key="lm-studio",
        label="LM Studio Cache",
        path="~/.cache/lm-studio",
    ),
    CachePathDef(
        key="torch",
        label="PyTorch Kernel Cache",
        path="~/.cache/torch/kernels",
    ),
    CachePathDef(
        key="triton",
        label="OpenAI Triton Cache",
        path="~/.triton/cache",
    ),
    CachePathDef(
        key="cuda",
        label="NVIDIA CUDA Cache",
        path="~/.nv/ComputeCache",
    ),
)

AI_MODEL_CLEANUP_DEFS = (
    AgeCleanupDef(
        key="huggingface",
        label="HuggingFace Hub",
        path="~/.cache/huggingface/hub",
        age_days=14,
    ),
    AgeCleanupDef(
        key="ollama-blobs",
        label="Ollama Blobs",
        path="~/.ollama/models/blobs",
        age_days=14,
    ),
    AgeCleanupDef(
        key="torch",
        label="PyTorch Kernel Cache",
        path="~/.cache/torch/kernels",
        age_days=7,
    ),
    AgeCleanupDef(
        key="triton",
        label="OpenAI Triton Cache",
        path="~/.triton/cache",
        age_days=7,
    ),
    AgeCleanupDef(
        key="cuda",
        label="NVIDIA CUDA Cache",
        path="~/.nv/ComputeCache",
        age_days=7,
    ),
    AgeCleanupDef(
        key="lm-studio",
        label="LM Studio Cache",
        path="~/.cache/lm-studio",
        age_days=7,
    ),
)


def get_analyze_cache_defs() -> tuple[CachePathDef, ...]:
    return (*PACKAGE_MANAGER_CACHE_DEFS, *CONTAINER_CACHE_DEFS, *AI_MODEL_CACHE_DEFS)


def get_package_manager_cleaner(os_id: str) -> PackageManagerCleanerDef | None:
    return next(
        (definition for definition in PACKAGE_MANAGER_CLEANER_DEFS if os_id in definition.os_ids),
        None,
    )


def get_container_cache_def(key: str) -> CachePathDef:
    for definition in CONTAINER_CACHE_DEFS:
        if definition.key == key:
            return definition
    raise KeyError(f"Unknown container cache definition: {key}")


def get_ai_model_cleanup_defs() -> tuple[AgeCleanupDef, ...]:
    return AI_MODEL_CLEANUP_DEFS
