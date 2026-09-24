import re
import shutil
from pathlib import Path

from ..core.constants import (
    CLEAN_CARGO_AGE_DAYS,
    DEFAULT_PROJECT_SEARCH_PATHS,
    DEV_CACHES,
    OK,
    SKIP,
    WARN,
)
from ..core.file_ops import (
    SI_MULTIPLIER,
    clean_path_by_age,
    get_size_fast,
    register_cleaned_path,
    safe_remove,
)
from ..core.heavy_cache import get_ai_model_cleanup_defs, get_container_cache_def
from ..core.render import bytes_to_human
from ..core.system import run_command
from .report import report_command_failure
from .totals import as_totals

# docker, podman and multipass are all just front-ends to a daemon that does the
# real work. When the CLI is SIGKILLed on timeout the daemon keeps deleting to
# completion, so a timeout here is not "the work failed" -- it is "we stopped
# waiting for the client while the server carries on". These prunes used to
# inherit the 300 s DEFAULT_COMMAND_TIMEOUT by omission; a docker system prune on
# a busy host can run past that, and reporting the resulting SIGKILL as a failure
# said the opposite of what happened on disk. The generous ceiling keeps a normal
# prune from ever hitting it, and the timeout branch (see clean_docker) reports
# "still running in the background" rather than failure when it does.
DAEMON_PRUNE_TIMEOUT = 600


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
            print(f"  {SKIP} {description} ({bytes_to_human(total_size)}) would be cleaned")
            return total_size, 1
        return 0, 0

    if total_size > 0 or not cache_path:
        res = run_command(command_args, capture=True)
        cache_gone = cache_path and not Path(cache_path).expanduser().exists()
        if res.ok or cache_gone:
            # Report space actually reclaimed (before - after), not the pre-clean
            # size, since `npm/pip/go cache clean` may only clear part of it.
            freed = total_size
            if cache_path and not cache_gone:
                after = get_size_fast(Path(cache_path).expanduser())
                freed = max(0, total_size - after)
            print(f"  {OK} {description} ({bytes_to_human(freed)}) cleaned")
            return freed, 1
    return 0, 0


# docker's own total, the closing line of a prune. go-units' HumanSize divides by
# 1000 and writes the kilo prefix lowercase ("1.653GB", "500kB", "0B"), hence
# SI_MULTIPLIER rather than the binary powers. Anchored on the sentence because
# the "Deleted Images:" block above it lists sha256 digests, and a whole-
# transcript read would answer with whatever size those hex digits look like.
_DOCKER_RECLAIMED_SPACE = re.compile(r"Total reclaimed space:\s*([0-9.]+)\s*([kMGTPE]?)B")


def _docker_reclaimed_bytes(output: str) -> int:
    """Bytes docker says its prune reclaimed, 0 when it did not say."""
    match = _DOCKER_RECLAIMED_SPACE.search(output)
    if not match:
        return 0
    return int(float(match.group(1)) * SI_MULTIPLIER[match.group(2)])


def clean_docker(dry_run=False):
    """Clean the rebuildable half of Docker's disk usage."""
    if shutil.which("docker"):
        if dry_run:
            print(f"  {SKIP} Docker (unused images/build cache) would be pruned")
            return 0, 1
        use_sudo = True
        if run_command(["docker", "info"], capture=True, timeout=10).ok:
            use_sudo = False
        # No `--volumes`. -f already turns off docker's own "are you sure"
        # listing, so this runs unattended; `--volumes` would then delete every
        # volume no *running* container holds -- a stopped compose stack's
        # database, a service's upload directory -- which is user data, not a
        # cache, and nothing this module cleans gets a trash copy (see
        # clean_developer_tools' closing comment). It also does not mean the same
        # volume set across docker releases. What is pruned here -- stopped
        # containers, dangling images, unused networks, build cache -- all
        # rebuilds itself; pruning volumes needs its own reviewed flow that names
        # them first. podman's prune below never carried the flag either.
        res = run_command(
            ["docker", "system", "prune", "-f"],
            use_sudo=use_sudo,
            capture=True,
            timeout=DAEMON_PRUNE_TIMEOUT,
        )
        if res.ok:
            freed = _docker_reclaimed_bytes(res.stdout)
            freed_str = f" ({bytes_to_human(freed)})" if freed else ""
            print(f"  {OK} Docker system pruned{freed_str}")
            return freed, 1
        if res.timed_out:
            # The CLI was SIGKILLed, but dockerd goes on deleting; this is unknown,
            # not failed, so it neither reports an error nor claims a byte count.
            print(f"  {WARN} Docker prune is still reclaiming space in the background")
            return 0, 0
        report_command_failure("Docker prune", res)
    return 0, 0


def clean_podman(dry_run=False):
    """Clean unused Podman data and caches."""
    total_size = 0
    items = 0
    if shutil.which("podman"):
        if dry_run:
            print(f"  {SKIP} Podman (unused images/build cache) would be pruned")
            items += 1
        else:
            res = run_command(
                ["podman", "system", "prune", "-f"], capture=True, timeout=DAEMON_PRUNE_TIMEOUT
            )
            if res.ok:
                print(f"  {OK} Podman system pruned")
                items += 1
            elif res.timed_out:
                # Same as docker: the client died on timeout, the service keeps
                # pruning. Not counted as a completed prune, not reported failed.
                print(f"  {WARN} Podman prune is still reclaiming space in the background")
            else:
                report_command_failure("Podman prune", res)

        # Clean storage cache
        cache_path = get_container_cache_def("podman-cache").resolved_path()
        if cache_path.exists():
            register_cleaned_path(cache_path)
            s, i = clean_path_by_age(cache_path, days=0, dry_run=dry_run)
            total_size += s
            items += i
            if i > 0 and not dry_run:
                print(f"  {OK} Podman transfer cache ({bytes_to_human(s)}) cleaned")
    return total_size, items


def clean_multipass(dry_run=False):
    """Purges deleted Multipass instances."""
    if shutil.which("multipass"):
        if dry_run:
            print(f"  {SKIP} Multipass deleted instances would be purged")
            return 0, 1
        res = run_command(["multipass", "purge"], capture=True, timeout=DAEMON_PRUNE_TIMEOUT)
        if res.ok:
            print(f"  {OK} Multipass purged")
            return 0, 1
        if res.timed_out:
            # multipassd carries the purge on past the killed client.
            print(f"  {WARN} Multipass purge is still running in the background")
            return 0, 0
        report_command_failure("Multipass purge", res)
    return 0, 0


def clean_ai_models(dry_run=False):
    """Clean heavy AI model hubs with age awareness."""
    total_size = 0
    total_items = 0

    for target in get_ai_model_cleanup_defs():
        path = target.resolved_path()
        register_cleaned_path(path)
        s, i = clean_path_by_age(path, days=target.age_days, dry_run=dry_run)
        if i > 0:
            total_size += s
            total_items += i
            glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
            print(f"  {glyph} {target.label} ({bytes_to_human(s)}) {status}")
    return total_size, total_items


def clean_java_caches(dry_run=False):
    """Clean Gradle and Maven build caches."""
    total_size = 0
    total_items = 0
    home = Path.home()

    targets = [
        ("Gradle caches", home / ".gradle" / "caches"),
        ("Gradle wrapper", home / ".gradle" / "wrapper" / "dists"),
        ("Maven repository", home / ".m2" / "repository"),
    ]
    for label, cache_path in targets:
        if not cache_path.is_dir():
            continue
        register_cleaned_path(cache_path)
        s, i = clean_path_by_age(cache_path, days=60, dry_run=dry_run)
        if i > 0:
            total_size += s
            total_items += i
            glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
            print(f"  {glyph} {label} ({bytes_to_human(s)}) {status}")
    return total_size, total_items


def clean_python_pycache(dry_run=False):
    """Clean __pycache__ directories from user project directories."""
    total_size = 0
    total_items = 0

    for search_path in DEFAULT_PROJECT_SEARCH_PATHS:
        root = Path(search_path)
        if not root.is_dir():
            continue
        try:
            for pycache in root.rglob("__pycache__"):
                if not pycache.is_dir():
                    continue
                size = get_size_fast(pycache)
                if safe_remove(pycache, use_trash=False, dry_run=dry_run)[0]:
                    total_size += size
                    total_items += 1
        except OSError:
            continue

    if total_items > 0:
        glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
        print(f"  {glyph} Python __pycache__ ({bytes_to_human(total_size)}) {status}")
    return total_size, total_items


def clean_package_manager_caches(dry_run: bool = False) -> tuple[int, int]:
    """Clean standard developer package manager caches (npm, pip, go)."""
    total_size = 0
    total_items = 0
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
    return total_size, total_items


def clean_cargo_cache(dry_run: bool = False) -> tuple[int, int]:
    """Clean Rust Cargo registry cache based on age."""
    cargo_path = DEV_CACHES["cargo"]
    if not cargo_path.exists():
        return 0, 0

    register_cleaned_path(cargo_path)
    s, i = clean_path_by_age(cargo_path, days=CLEAN_CARGO_AGE_DAYS, dry_run=dry_run)
    if i > 0:
        glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")
        print(f"  {glyph} Cargo cache ({bytes_to_human(s)}) {status}")
        return s, i
    return 0, 0


def clean_container_and_virtualization_caches(dry_run: bool = False) -> tuple[int, int]:
    """Clean Docker, Podman, and Multipass virtualization caches."""
    total_size = 0
    total_items = 0
    for func in [clean_docker, clean_podman, clean_multipass]:
        s, i = func(dry_run=dry_run)[:2]
        if i > 0:
            total_size += s
            total_items += i
    return total_size, total_items


def clean_developer_tools(dry_run: bool = False) -> tuple[int, int, int, int]:
    """Main entry for developer-focused cleanup pipeline."""
    total_size = 0
    total_items = 0
    total_categories = 0

    dev_sub_cleaners = [
        clean_package_manager_caches,
        clean_cargo_cache,
        clean_java_caches,
        clean_python_pycache,
        clean_ai_models,
        clean_container_and_virtualization_caches,
    ]

    for cleaner in dev_sub_cleaners:
        s, i, _, _ = as_totals(cleaner(dry_run=dry_run))
        if i > 0:
            total_size += s
            total_items += i
            total_categories += 1

    # Every developer artifact here is a rebuildable cache and is unlinked
    # outright, so none of these bytes are sitting in the trash.
    return total_size, total_items, total_categories, 0
