"""Shared metadata for heavyweight cache families.

Analyze uses these definitions to surface large cache roots. Clean owns the
actual cleanup actions because package managers, containers, and model tools
usually need command-specific behavior rather than direct path deletion.
"""

import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CachePathDef:
    key: str
    label: str
    path: str
    min_display_bytes: int = 10 * 1024 * 1024
    icon: str = "👀"

    def resolved_path(self) -> Path:
        raw_p = Path(self.path).expanduser()
        if not raw_p.exists() and self.key == "dnf":
            for dnf5_path in (Path("/var/cache/libdnf5"), Path("/var/cache/dnf5daemon-server")):
                if dnf5_path.exists():
                    return dnf5_path
        return raw_p


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
        icon="📦",
    ),
    CachePathDef(
        key="pacman",
        label="Pacman Cache",
        path="/var/cache/pacman/pkg",
        icon="📦",
    ),
    CachePathDef(
        key="dnf",
        label="Dnf Cache",
        path="/var/cache/dnf",
        icon="📦",
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
        icon="🐳",
    ),
    CachePathDef(
        key="docker-system",
        label="Docker System",
        path="/var/lib/docker",
        icon="🐳",
    ),
    CachePathDef(
        key="podman-cache",
        label="Podman Transfer Cache",
        path="~/.cache/containers",
        icon="🦭",
    ),
    CachePathDef(
        key="flatpak-data",
        label="Flatpak App Data",
        path="~/.local/share/flatpak",
        icon="📦",
    ),
)

AI_MODEL_CACHE_DEFS = (
    CachePathDef(
        key="ollama-models",
        label="Ollama Models",
        path="~/.ollama/models",
        icon="🤖",
    ),
    CachePathDef(
        key="huggingface",
        label="HuggingFace Hub",
        path="~/.cache/huggingface/hub",
        icon="🤗",
    ),
    CachePathDef(
        key="lm-studio",
        label="LM Studio Cache",
        path="~/.cache/lm-studio",
        icon="🤖",
    ),
    CachePathDef(
        key="torch",
        label="PyTorch Kernel Cache",
        path="~/.cache/torch/kernels",
        icon="🔥",
    ),
    CachePathDef(
        key="triton",
        label="OpenAI Triton Cache",
        path="~/.triton/cache",
        icon="🤖",
    ),
    CachePathDef(
        key="cuda",
        label="NVIDIA CUDA Cache",
        path="~/.nv/ComputeCache",
        icon="⚡",
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
    # Exclude flatpak-data from Analyze Disk root view to avoid duplicate display & stats with Home (~/.local/share/flatpak)
    return tuple(
        d
        for d in (*PACKAGE_MANAGER_CACHE_DEFS, *CONTAINER_CACHE_DEFS, *AI_MODEL_CACHE_DEFS)
        if d.key != "flatpak-data"
    )


def get_package_manager_cleaner(os_id: str) -> PackageManagerCleanerDef | None:
    # 1. Match by exact os_id if specified in definition
    for definition in PACKAGE_MANAGER_CLEANER_DEFS:
        if os_id in definition.os_ids:
            return definition
    # 2. Fallback to executable tool presence (covers derivatives), but skip if os_id is 'unknown' (e.g. tests)
    if os_id != "unknown":
        for definition in PACKAGE_MANAGER_CLEANER_DEFS:
            if shutil.which(definition.executable):
                return definition
    return None


def get_container_cache_def(key: str) -> CachePathDef:
    for definition in CONTAINER_CACHE_DEFS:
        if definition.key == key:
            return definition
    raise KeyError(f"Unknown container cache definition: {key}")


def get_ai_model_cleanup_defs() -> tuple[AgeCleanupDef, ...]:
    return AI_MODEL_CLEANUP_DEFS
