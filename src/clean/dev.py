import shutil
import subprocess
from pathlib import Path

from ..core.constants import DEV_CACHES
from ..core.file_ops import (
    bytes_to_human,
    clean_path_by_age,
    get_size_fast,
    register_cleaned_path,
    safe_remove,
)
from ..core.system import run_command


def clean_tool_cache(description, command_args, cache_path=None, dry_run=False):
    """Helper to clean a specific tool's cache with verified success."""
    total_size = 0
    if cache_path:
        path = Path(cache_path).expanduser()
        if path.exists():
            total_size = get_size_fast(path)
        register_cleaned_path(cache_path)

    if dry_run:
        if total_size > 0 or not cache_path:
            print(
                f"  \033[0;32m✓\033[0m {description} ({bytes_to_human(total_size)}) would be cleaned"
            )
            return total_size, 1
        return 0, 0

    if total_size > 0 or not cache_path:
        res = run_command(command_args, capture=True)
        if (res and res.returncode == 0) or (
            cache_path and not Path(cache_path).expanduser().exists()
        ):
            print(f"  \033[0;32m✓\033[0m {description} ({bytes_to_human(total_size)}) cleaned")
            return total_size, 1
    return 0, 0


def clean_docker(dry_run=False):
    """Clean unused Docker data."""
    if shutil.which("docker"):
        if dry_run:
            print("  \033[0;32m✓\033[0m Docker (unused images/volumes) would be pruned")
            return 0, 1
        use_sudo = True
        try:
            if subprocess.run(["docker", "info"], capture_output=True).returncode == 0:
                use_sudo = False
        except Exception:
            pass
        res = run_command(
            ["docker", "system", "prune", "-f", "--volumes"], use_sudo=use_sudo, capture=True
        )
        if res and res.returncode == 0:
            print("  \033[0;32m✓\033[0m Docker system pruned")
            return 0, 1
    return 0, 0


def clean_podman(dry_run=False):
    """Clean unused Podman data and caches."""
    total_size = 0
    items = 0
    if shutil.which("podman"):
        if dry_run:
            print("  \033[0;32m✓\033[0m Podman (unused images/volumes) would be pruned")
            items += 1
        else:
            res = run_command(["podman", "system", "prune", "-f"], capture=True)
            if res and res.returncode == 0:
                print("  \033[0;32m✓\033[0m Podman system pruned")
                items += 1

        # Clean storage cache
        cache_path = Path.home() / ".cache/containers"
        if cache_path.exists():
            register_cleaned_path(cache_path)
            s, i = clean_path_by_age(cache_path, days=0, dry_run=dry_run)
            total_size += s
            items += i
            if i > 0 and not dry_run:
                print(f"  \033[0;32m✓\033[0m Podman transfer cache ({bytes_to_human(s)}) cleaned")
    return total_size, items


def clean_multipass(dry_run=False):
    """Purges deleted Multipass instances."""
    if shutil.which("multipass"):
        if dry_run:
            print("  \033[0;32m✓\033[0m Multipass deleted instances would be purged")
            return 0, 1
        res = run_command(["multipass", "purge"], capture=True)
        if res and res.returncode == 0:
            print("  \033[0;32m✓\033[0m Multipass purged")
            return 0, 1
    return 0, 0


def clean_ai_models(dry_run=False):
    """Clean heavy AI model hubs with age awareness."""
    total_size = 0
    total_items = 0

    targets = [
        (DEV_CACHES["huggingface"], "HuggingFace Hub", 14),
        (DEV_CACHES["ollama"], "Ollama Blobs", 14),
        (DEV_CACHES["torch"], "PyTorch Kernel Cache", 7),
        (DEV_CACHES["triton"], "OpenAI Triton Cache", 7),
        (DEV_CACHES["cuda"], "NVIDIA CUDA Cache", 7),
        (Path.home() / ".cache/lm-studio", "LM Studio Cache", 7),
    ]

    for path, desc, days in targets:
        register_cleaned_path(path)
        s, i = clean_path_by_age(path, days=days, dry_run=dry_run)
        if i > 0:
            total_size += s
            total_items += i
            status = "would be cleaned" if dry_run else "cleaned"
            print(f"  \033[0;32m✓\033[0m {desc} ({bytes_to_human(s)}) {status}")
    return total_size, total_items


def clean_developer_tools(dry_run=False):
    """Main entry for developer-focused cleanup."""
    total_size = 0
    total_items = 0
    total_categories = 0

    # 1. Standard package managers
    pm_tools = [
        ("npm cache", ["npm", "cache", "clean", "--force"], DEV_CACHES["npm"]),
        ("pip cache", ["pip3", "cache", "purge"], DEV_CACHES["pip"]),
        ("go cache", ["go", "clean", "-cache"], DEV_CACHES["go"]),
    ]
    for desc, cmd, path in pm_tools:
        if shutil.which(cmd[0]):
            s, i = clean_tool_cache(desc, cmd, path, dry_run)
            if i > 0:
                total_size += s
                total_items += i
                total_categories += 1

    # 2. Cargo registry (custom removal)
    cargo_path = DEV_CACHES["cargo"]
    if cargo_path.exists():
        register_cleaned_path(cargo_path)
        size = get_size_fast(cargo_path)
        if size > 1024:
            removed = dry_run
            if dry_run:
                print(f"  \033[0;32m✓\033[0m Cargo cache ({bytes_to_human(size)}) would be cleaned")
            else:
                removed = safe_remove(cargo_path, use_trash=False)[0]
                if removed:
                    print(f"  \033[0;32m✓\033[0m Cargo cache ({bytes_to_human(size)}) cleaned")
            if removed:
                total_size += size
                total_items += 1
                total_categories += 1

    # 3. AI & Virtualization
    for func in [clean_ai_models, clean_docker, clean_podman, clean_multipass]:
        s, i = func(dry_run=dry_run)[:2]
        if i > 0:
            total_size += s
            total_items += i
            total_categories += 1
    return total_size, total_items, total_categories
