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
    icon: str = "👀"
    # Where the same data lives when the primary path is absent: dnf5 moved its
    # cache, and a snap-packaged Docker keeps its data under /var/snap.
    fallback_paths: tuple[str, ...] = ()

    def resolved_path(self) -> Path:
        raw_p = Path(self.path).expanduser()
        if raw_p.exists():
            return raw_p
        for candidate in self.fallback_paths:
            candidate_path = Path(candidate).expanduser()
            if candidate_path.exists():
                return candidate_path
        return raw_p


@dataclass(frozen=True)
class AgeCleanupDef:
    key: str
    label: str
    path: str
    age_days: int

    def resolved_path(self) -> Path:
        return Path(self.path).expanduser()


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
        key="zypper",
        label="Zypper Cache",
        path="/var/cache/zypp/packages",
        icon="📦",
    ),
    CachePathDef(
        key="dnf",
        label="Dnf Cache",
        path="/var/cache/dnf",
        icon="📦",
        fallback_paths=("/var/cache/libdnf5", "/var/cache/dnf5daemon-server"),
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
        fallback_paths=("/var/snap/docker/common/var-lib-docker",),
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
    # Ubuntu's largest block outside Home and /usr: snapd keeps up to two
    # squashfs revisions per snap under snaps/, plus a download cache. Display
    # only -- /var/lib/snapd is outside the cleanable allowlist, so deletion is
    # refused, and `snap remove --revision` in Clean owns the actual cleanup.
    CachePathDef(
        key="snapd",
        label="Snap Packages",
        path="/var/lib/snapd",
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
    # Exclude flatpak-data and dnf from Analyze Disk root view to avoid duplicate display & stats with Home / system metadata
    return tuple(
        d
        for d in (*PACKAGE_MANAGER_CACHE_DEFS, *CONTAINER_CACHE_DEFS, *AI_MODEL_CACHE_DEFS)
        if d.key not in ("flatpak-data", "dnf")
    )


def get_container_cache_def(key: str) -> CachePathDef:
    for definition in CONTAINER_CACHE_DEFS:
        if definition.key == key:
            return definition
    raise KeyError(f"Unknown container cache definition: {key}")


def get_ai_model_cleanup_defs() -> tuple[AgeCleanupDef, ...]:
    return AI_MODEL_CLEANUP_DEFS
