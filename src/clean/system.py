import platform
import re
import shutil
from pathlib import Path

from ..core.constants import OK, SKIP
from ..core.file_ops import (
    bytes_to_human,
    get_size_fast,
    parse_size_from_text,
    record_deletion_audit,
    safe_remove,
)
from ..core.heavy_cache import PACKAGE_MANAGER_CACHE_DEFS
from ..core.lock import is_file_locked
from ..core.package_manager import detect_package_manager, resolve_admin_tool
from ..core.system import (
    APT_NONINTERACTIVE_ENV,
    C_LOCALE_ENV,
    get_os_id,
    run_command,
)
from ..core.text import plural
from ..core.whitelist import is_system_cleanable_content


class DryRunReporter:
    """Helper to handle uniform output reporting across dry-run and actual execution modes."""

    @staticmethod
    def report(
        action_name: str,
        freed_bytes: int = 0,
        items_count: int = 0,
        dry_run: bool = False,
    ) -> tuple[int, int, int]:
        if freed_bytes == 0 and items_count == 0:
            return 0, 0, 0

        size_str = f" ({bytes_to_human(freed_bytes)})" if freed_bytes > 0 else ""
        items_str = f" ({items_count} items)" if items_count > 0 and freed_bytes == 0 else ""

        # A preview line must not wear the glyph a finished delete wears. `✓ ...
        # would be cleaned` differed from `✓ Cleaned ...` by a verb tense alone,
        # which survives neither a skim nor a paste with the colors stripped; `◎`
        # says "it is there, this run left it alone" on its own. Every dry-run
        # line in clean/ pairs SKIP with the conditional tense for that reason.
        if dry_run:
            print(f"  {SKIP} {action_name}{size_str}{items_str} would be cleaned")
        else:
            print(f"  {OK} Cleaned {action_name}{size_str}{items_str}")

        return freed_bytes, items_count, 1


def clean_snaps(dry_run: bool = False) -> tuple[int, int, int]:
    """Removes old revisions of snaps to save massive space on Ubuntu."""
    if not shutil.which("snap"):
        return 0, 0, 0

    if dry_run:
        print(f"  {SKIP} Old Snap revisions would be removed")
        return 0, 0, 1

    # The revision table is matched on the English word "disabled".
    res = run_command(["snap", "list", "--all"], capture=True, env=C_LOCALE_ENV)
    if not res or not res.stdout:
        return 0, 0, 0

    count = 0
    for line in res.stdout.splitlines():
        if "disabled" in line:
            parts = line.split()
            if len(parts) >= 3:
                rm_res = run_command(
                    ["snap", "remove", parts[0], "--revision", parts[2]],
                    use_sudo=True,
                    capture=True,
                )
                if rm_res.ok:
                    count += 1

    if count > 0:
        print(f"  {OK} Removed {count} old Snap revisions")
        return 0, count, 1
    return 0, 0, 0


def _get_package_manager_cache_paths(cleaner_key: str) -> list[Path]:
    """The cache roots to look in, from the table Analyze reads too.

    Every path of the family that exists, not just the first: dnf5 moved the
    cache to /var/cache/libdnf5 and leaves the old /var/cache/dnf behind, so both
    have to be looked at; apt's two pkgcache.bin indexes sit beside archives/ and
    go in the same `apt-get clean`. apt's own `partial/` subdirectory is
    deliberately not listed -- get_size_fast() already recurses into it, so
    naming it separately only counted its bytes twice.

    For apt these roots *are* what gets emptied, so measuring them measures the
    clean. For dnf they are not: see _repo_cache_cleanable_paths().
    """
    for definition in PACKAGE_MANAGER_CACHE_DEFS:
        if definition.key == cleaner_key:
            candidates = (
                definition.path,
                *definition.fallback_paths,
                *definition.extra_paths,
            )
            return [path for path in map(Path, candidates) if path.exists()]
    return []


def _measure_package_cache_size(cache_paths: list[Path]) -> int:
    """Measures total bytes held by package manager cache paths.

    Directories and plain files alike -- get_size_fast() stats a file rather than
    scanning it, which is what apt's two binary indexes need.
    """
    return sum(get_size_fast(p) for p in cache_paths if p.exists())


# A libdnf cache directory is named "<repo id>-<16 hex digits of its config>".
_DNF_REPO_CACHE_DIR = re.compile(r"^(?P<repo_id>.+)-[0-9a-f]{16}$")
# What `dnf clean packages dbcache` empties inside each repository's directory:
# the downloaded rpms and the solv indexes generated from repodata. repodata/
# itself is deliberately absent -- deleting it is what forces a re-download.
_DNF_CLEANED_SUBDIRS = ("packages", "solv")
# Where dnf4 keeps the same generated indexes: loose files beside the repository
# directories rather than inside them. Listed so a dnf4 box measures its dbcache
# too, and harmless on dnf5, where no such file exists.
_DNF4_DBCACHE_SUFFIXES = (".solv", ".solvx")
# PackageKit -- what GNOME Software and Discover talk to -- keeps a third copy of
# the same thing, and no package-manager command empties it: 200 MiB on the
# machine this was written for. Its rpm backend is libdnf, so the layout is the
# one above, just buried under <releasever>/metadata/, which is why the search
# below descends instead of listing one level.
_PACKAGEKIT_CACHE = Path("/var/cache/PackageKit")
# How far to descend looking for repository directories. 3 reaches PackageKit's
# "44/metadata/<repo>"; nothing legitimate is deeper, and a bound keeps this from
# ever walking into a repository's own contents.
_REPO_CACHE_SEARCH_DEPTH = 3
# rpm's transaction lock, and the only trustworthy answer to "is a package
# transaction happening right now". The daemon that owns one of these caches is
# dbus-activated but does not exit promptly -- on the machine this was written
# for it had been idle for thirteen hours -- so "is the process alive" would
# have parked the sweep forever, which is the same never-fires bug in a new
# place. The lock is held for the install itself, the window worth waiting out.
_RPM_TRANSACTION_LOCK = Path("/var/lib/rpm/.rpm.lock")
# An update downloaded now and installed at the next boot leaves its packages
# sitting in one of these caches in the meantime, so the sweep has to stand down
# while one is staged. systemd's marker and PackageKit's are unambiguous; dnf5
# instead records a status, and only two of its values mean "staged": its own
# `offline status` prints "run `dnf5 offline reboot`" for download-complete and
# ready, and for transaction-incomplete -- what a failed attempt leaves behind
# for months -- it prints a post-mortem instead. Reading the file rather than
# tomllib because the tests still run on 3.10, where tomllib does not exist.
_OFFLINE_UPDATE_MARKERS = (Path("/system-update"), Path("/var/lib/PackageKit/prepared-update"))
_OFFLINE_TRANSACTION_STATE = Path(
    "/usr/lib/sysimage/libdnf5/offline/offline-transaction-state.toml"
)
_OFFLINE_STATUS_LINE = re.compile(r'^\s*status\s*=\s*"([^"]*)"', re.MULTILINE)
_OFFLINE_STAGED_STATUSES = frozenset({"ready", "download-complete"})


def _offline_update_is_staged() -> bool:
    """Whether an update is downloaded and waiting for the reboot that installs it."""
    if any(marker.exists() for marker in _OFFLINE_UPDATE_MARKERS):
        return True
    try:
        state = _OFFLINE_TRANSACTION_STATE.read_text(errors="replace")
    except FileNotFoundError:
        # No transaction was ever recorded, which is the ordinary case.
        return False
    except OSError:
        # There is a state file and it cannot be read. "Cannot tell" is not
        # "nothing staged", and the file dnf5 writes is world-readable, so this
        # is a strange machine rather than a common one: leave the cache alone.
        return True
    status = _OFFLINE_STATUS_LINE.search(state)
    return bool(status and status.group(1) in _OFFLINE_STAGED_STATUSES)


def _repo_cache_dirs(cache_roots: list[Path]) -> list[tuple[str, Path]]:
    """Every "<repo id>-<hash>" directory under *cache_roots*, with its repo id.

    The same libdnf layout sits at different depths depending on who wrote it:
    /var/cache/libdnf5/<repo>, but /var/cache/PackageKit/<releasever>/metadata/
    <repo>. Descent stops at the first match, so nothing inside a repository's
    own directory can be read as another repository.
    """
    found: list[tuple[str, Path]] = []
    frontier = [(root, 0) for root in cache_roots]
    while frontier:
        directory, depth = frontier.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir():
                continue
            match = _DNF_REPO_CACHE_DIR.match(entry.name)
            if match:
                found.append((match["repo_id"], entry))
            elif depth < _REPO_CACHE_SEARCH_DEPTH:
                frontier.append((entry, depth + 1))
    # Sorted because the walk is depth-first over several roots: the order it
    # finds things in is an implementation detail, and this list decides the
    # order of an `rm` argv and of the audit lines that follow it.
    found.sort(key=lambda pair: pair[1])
    return found


def _repo_cache_cleanable_paths(cache_roots: list[Path]) -> list[Path]:
    """The paths a `clean packages dbcache` reaches, or would if it could reach here.

    Measuring the cache roots instead is what made the preview lie: it counted
    repodata/ -- on a default Fedora nearly the whole of the cache, since
    keepcache is false and there are no rpms to count -- and then promised those
    bytes to a command that never touches them. `topo clean --dry-run` said
    1.2 GiB and the real run freed nothing, which is exactly what it should have
    said it would.
    """
    paths = [
        subdir
        for _, repo_dir in _repo_cache_dirs(cache_roots)
        for name in _DNF_CLEANED_SUBDIRS
        if (subdir := repo_dir / name).is_dir()
    ]
    for root in cache_roots:
        try:
            entries = sorted(root.iterdir())
        except OSError:
            continue
        paths.extend(e for e in entries if e.is_file() and e.suffix in _DNF4_DBCACHE_SUFFIXES)
    return paths


def _dnf_orphaned_repo_caches(tool: str, cache_roots: list[Path]) -> list[Path]:
    """Cache directories belonging to repositories that are no longer configured.

    dnf only ever cleans the repositories it still knows about, so deleting a
    .repo file strands its entire cache directory -- repodata, dbcache and all --
    with nothing left that will ever collect it. Nothing is re-downloaded for a
    repository that no longer exists, which is why these go even though the
    repodata of a live repository stays.

    Every uncertainty is resolved as "not an orphan": an unreadable root, a
    repolist that failed, a directory name that does not carry the id-hash shape.
    A repository whose *configuration* changed keeps its id, so its stale
    directory is left behind rather than risk reading dnf's answer too freely.
    """
    if not cache_roots:
        return []
    res = run_command([tool, "repolist", "--all"], capture=True, env=C_LOCALE_ENV)
    if not res.ok or not res.stdout:
        return []
    # First column of dnf's own table; ids never contain whitespace. The header
    # and any summary line contribute junk entries, which can only ever make a
    # directory look configured -- the safe direction.
    configured = {line.split()[0] for line in res.stdout.splitlines() if line.split()}

    orphans: list[Path] = []
    for repo_id, repo_dir in _repo_cache_dirs(cache_roots):
        # "@commandline" and friends are dnf's own pseudo-repositories; they
        # never appear in repolist and are not anybody's leftovers.
        if repo_id.startswith("@") or repo_id in configured:
            continue
        # The /var carve-out decides, not this function: only what the
        # whitelist already calls cleanable system content may be removed.
        if is_system_cleanable_content(repo_dir):
            orphans.append(repo_dir)
    return orphans


def _remove_unreachable_cache_paths(paths: list[Path]) -> None:
    """Delete the package caches no clean command reaches.

    Three kinds end up here. dnf5daemon-server -- what GNOME Software installs
    through on Fedora -- keeps its own cachedir, and no dnf CLI command empties
    it: 881 MiB of downloaded rpms had collected there on the machine this was
    written for, every byte promised by the preview and none of it reachable by
    the clean. PackageKit keeps a third copy, 200 MiB of it, belonging to no
    command at all. Stranded repository directories are the same problem
    arriving by a different road.

    All root's to remove, and none of it goes to the trash: /var/cache is
    outside the trash spec, and everything here is a cache by definition -- an
    rpm re-downloads, a solv index regenerates.

    One `rm` for the lot, then each path is asked whether it is actually gone, so
    a partial failure is audited as the partial failure it was.
    """
    targets = [p for p in paths if p.exists() and is_system_cleanable_content(p)]
    if not targets:
        return
    sizes = {path: get_size_fast(path) for path in targets}
    run_command(
        ["rm", "-rf", "--one-file-system", "--", *(str(path) for path in targets)],
        use_sudo=True,
        capture=True,
    )
    for path in targets:
        removed = not path.exists()
        record_deletion_audit(
            path,
            "sudo-permanent",
            "removed" if removed else "failed",
            sizes[path] if removed else None,
        )


def clean_package_manager(dry_run: bool = False) -> tuple[int, int, int]:
    """Clean system package manager caches."""
    manager = detect_package_manager(get_os_id())
    if manager is None:
        return 0, 0, 0

    freed = 0
    snap_items = 0
    snap_cats = 0
    if manager.key == "apt":
        s, snap_items, snap_cats = clean_snaps(dry_run=dry_run)
        freed += s

    # The resolved tool, so a dnf5-only box is cleaned rather than skipped -- and
    # so the skip below asks about the binary that would actually run.
    tool = resolve_admin_tool(manager)
    if not shutil.which(tool):
        return freed, snap_items, snap_cats

    cache_paths = _get_package_manager_cache_paths(manager.key)
    # Caches laid out the libdnf way, holding per-repository packages/, solv/ and
    # repodata/. PackageKit's is one on every family and belongs to nobody's
    # clean command, so Topo clears it itself; on an rpm box dnf's own roots are
    # read the same way, because its command reaches only part of them.
    repo_style_roots = [_PACKAGEKIT_CACHE] if _PACKAGEKIT_CACHE.is_dir() else []
    if manager.key == "dnf":
        repo_style_roots = cache_paths + repo_style_roots
        # Nothing under dnf's roots is safe to promise wholesale, so the measured
        # set is built from the parts alone.
        measured_paths = _repo_cache_cleanable_paths(repo_style_roots)
        measured_paths += _dnf_orphaned_repo_caches(tool, repo_style_roots)
        sweepable = measured_paths
    else:
        # apt's roots *are* what `apt-get clean` empties, so they are promised as
        # they stand; only the PackageKit part is Topo's to remove. Orphans are
        # left alone here: finding them means asking the package manager which
        # repositories it still has, and `repolist` is dnf's word, not apt's.
        sweepable = _repo_cache_cleanable_paths(repo_style_roots)
        measured_paths = cache_paths + sweepable
    pre_size = _measure_package_cache_size(measured_paths)

    if dry_run:
        size_hint = f" ({bytes_to_human(pre_size)})" if pre_size > 0 else ""
        print(f"  {SKIP} {manager.label} cache{size_hint} would be cleaned")
        return freed + pre_size, snap_items, snap_cats + 1

    res = run_command(
        [tool, *manager.cache_clean_args], use_sudo=True, capture=True, env=C_LOCALE_ENV
    )
    if (
        sweepable
        and res.ok
        and not is_file_locked(_RPM_TRANSACTION_LOCK)
        and not _offline_update_is_staged()
    ):
        # Three conditions, each for its own reason: the package manager's own
        # clean went through, no transaction is installing out of the packages/
        # being swept, and no download is waiting for the reboot that installs it.
        _remove_unreachable_cache_paths(sweepable)
    post_size = _measure_package_cache_size(measured_paths)
    measured_freed = max(0, pre_size - post_size)

    if measured_freed > 0:
        freed += measured_freed
    elif res.ok and res.stdout:
        freed += parse_size_from_text(res.stdout)

    if res.ok:
        freed_str = f" ({bytes_to_human(freed)})" if freed > 0 else ""
        print(f"  {OK} Cleaned {manager.label} cache{freed_str}")
        return freed, snap_items + 1, snap_cats + 1

    return freed, snap_items, snap_cats


def clean_journal(dry_run: bool = False) -> tuple[int, int, int]:
    """Vacuum systemd journal logs."""
    if not shutil.which("journalctl"):
        return 0, 0, 0

    if dry_run:
        print(f"  {SKIP} journal logs would be vacuumed")
        return 0, 0, 1

    res = run_command(
        ["journalctl", "--vacuum-size=1M"], use_sudo=True, capture=True, env=C_LOCALE_ENV
    )
    if res.ok and res.stdout:
        freed = parse_size_from_text(res.stdout)
        if freed > 0:
            print(f"  {OK} Vacuumed journal logs ({bytes_to_human(freed)})")
            return freed, 1, 1
    return 0, 0, 0


def clean_orphaned_packages(dry_run: bool = False) -> tuple[int, int, int]:
    """Remove orphaned dependencies that are no longer needed."""
    manager = detect_package_manager(get_os_id())
    if manager is None or not manager.orphan_removal:
        return 0, 0, 0

    tool = resolve_admin_tool(manager)
    if not shutil.which(tool):
        return 0, 0, 0

    if manager.key == "apt":
        # `--dry-run` is unprivileged and narrates the very transaction `-y` would
        # run, so the count below is apt's own answer rather than a guess -- the
        # dry run used to promise "orphaned packages would be autoremoved" without
        # knowing whether there were any. The freed total is not available here:
        # apt prints "After this operation..." only when it really removes.
        preview = run_command(
            [tool, "autoremove", "--dry-run"], capture=True, env=APT_NONINTERACTIVE_ENV
        )
        orphans = _apt_removal_count(preview.stdout) if preview.ok else 0
        if not orphans:
            return 0, 0, 0
        if dry_run:
            print(
                f"  {SKIP} {plural(orphans, f'orphaned {manager.label} package')} would be removed"
            )
            return 0, 0, 1
        res = run_command(
            [tool, "autoremove", "-y"],
            use_sudo=True,
            capture=True,
            env=APT_NONINTERACTIVE_ENV,
        )
        if res.ok:
            # apt's own total, not parse_size_from_text over the whole transcript:
            # that takes the first size-looking token anywhere in it, and reads
            # apt's decimal kB/MB as binary ones (D7).
            freed = _apt_freed_bytes(res.stdout)
            print(
                f"  {OK} Removed {plural(orphans, f'orphaned {manager.label} package')}"
                f" ({bytes_to_human(freed)})"
            )
            return freed, orphans, 1

    elif manager.key == "dnf":
        if dry_run:
            # No number to promise here, unlike the apt branch above: `dnf
            # autoremove` takes the rpm transaction lock even to resolve, so there
            # is no unprivileged preview, and running it under sudo to get one is
            # the very thing --dry-run exists to avoid.
            print(f"  {SKIP} Orphaned {manager.label} packages would be autoremoved")
            return 0, 0, 1
        res = run_command([tool, "autoremove", "-y"], use_sudo=True, capture=True, env=C_LOCALE_ENV)
        if res.ok:
            # Both numbers come from dnf's transaction summary. What they replace:
            # parse_size_from_text() over the entire transcript, which returns the
            # first size-looking token anywhere in it -- on dnf5 that is the size
            # column of the first row of the package table, not the total (D7's
            # mistake, in the branch D7 did not touch) -- and `stdout.count("\n")
            # // 2`, half the number of lines dnf happened to print, which counts
            # the table, the progress narration and the trailing "Complete!" as
            # packages.
            freed = _dnf_freed_bytes(res.stdout)
            items = _dnf_removal_count(res.stdout)
            if not freed and not items:
                # "Nothing to do." -- and the same answer for a summary this cannot
                # read, where 0 is the honest report rather than a guess. Either way
                # there is nothing to claim, exactly as the apt branch claims nothing
                # when its preview finds no orphans.
                return 0, 0, 0
            print(
                f"  {OK} Removed {plural(items, f'orphaned {manager.label} package')}"
                f" ({bytes_to_human(freed)})"
            )
            return freed, items, 1

    elif manager.key == "pacman":
        list_res = run_command([tool, "-Qtdq"], capture=True)
        if list_res.ok and list_res.stdout.strip():
            orphans = list_res.stdout.split()
            if dry_run:
                print(
                    f"  {SKIP} {plural(len(orphans), f'orphaned {manager.label} package')} would be removed"
                )
                return 0, 0, 1
            remove_res = run_command(
                [tool, "-Rns", "--noconfirm"] + orphans,
                use_sudo=True,
                capture=True,
                env=C_LOCALE_ENV,
            )
            if remove_res.ok:
                freed = parse_size_from_text(remove_res.stdout)
                print(f"  {OK} Removed {plural(len(orphans), f'orphaned {manager.label} package')}")
                return freed, len(orphans), 1

    return 0, 0, 0


def clean_zombies(dry_run: bool = False) -> tuple[int, int, int]:
    """Identify and attempt to reap zombie processes."""
    # The state column is read as the English "Z" code.
    res = run_command(["ps", "-eo", "state,pid,ppid,comm"], capture=True, env=C_LOCALE_ENV)
    if not res.ok:
        return 0, 0, 0

    zombies = []
    for line in res.stdout.splitlines():
        if line.startswith("Z"):
            parts = line.split()
            if len(parts) >= 4:
                zombies.append({"pid": parts[1], "ppid": parts[2], "comm": parts[3]})

    if not zombies:
        return 0, 0, 0

    count = len(zombies)
    if dry_run:
        print(f"  {SKIP} {count} zombie processes detected")
        return 0, 0, 1

    parents = set(z["ppid"] for z in zombies)
    for ppid in parents:
        # Compare numerically, and only accept ASCII digits: a zero-padded "01"
        # or a Unicode digit form would slip past a string membership test and
        # send SIGCHLD to init (PID 1) or to the kernel's PID 0 placeholder.
        if not (ppid.isascii() and ppid.isdigit()):
            continue
        parent_pid = int(ppid)
        if parent_pid <= 1:
            continue
        run_command(["kill", "-SIGCHLD", str(parent_pid)], use_sudo=True, capture=True)

    print(f"  {OK} Signaled parents of {count} zombie processes")
    return 0, count, 1


# A kernel package carries its version in its name: linux-image-6.8.0-45-generic
# (Ubuntu), linux-image-6.1.0-18-amd64 (Debian). Names without one -- the
# linux-image-generic / linux-image-amd64 metapackages -- are not kernels.
_VERSIONED_KERNEL = re.compile(r"^linux-image-\d")
# What dpkg-query is asked for in place of `dpkg -l`'s table: the status pair the
# selection is made on, and the package name. Deliberately ${Package} and not
# ${binary:Package} -- kernel images are not Multi-Arch:same, and the name here is
# handed straight to `apt-get purge`, which wants it unqualified, exactly as the
# old fixed-width table printed it.
_DPKG_KERNEL_FORMAT = "${db:Status-Abbrev}\t${Package}\n"
# apt's own total for one operation. parse_size_from_text() cannot be pointed at
# the transcript: fed the whole thing it matches "2 to remove" as 2 TB. The
# sentence is English because the purge runs under APT_NONINTERACTIVE_ENV, and
# matching "freed" keeps the "additional disk space will be used" wording out.
_APT_FREED_SPACE = re.compile(
    r"After this operation, ([0-9.]+)\s*([kMGTPE]?)B disk space will be freed"
)
# apt divides by 1000, not 1024 (apt-pkg's SizeToStr), so these are the decimal
# multipliers rather than parse_size_from_text's binary ones. Every unit the
# pattern accepts needs an entry here, or a match would raise instead of parse.
_SI_MULTIPLIER = {
    "": 1,
    "k": 1000,
    "M": 1000**2,
    "G": 1000**3,
    "T": 1000**4,
    "P": 1000**5,
    "E": 1000**6,
}
# apt's per-package removal lines, the machine-readable half of its narration.
_APT_REMOVAL_LINE = re.compile(r"^(?:Remv|Purg) \S", re.MULTILINE)


def _kernel_version_key(name: str) -> tuple[int, ...]:
    """Every number in a kernel package name, as something sortable.

    "linux-image-6.8.0-100-generic" -> (6, 8, 0, 100), which orders after
    (6, 8, 0, 91). The prefix and the flavour suffix hold no digits, so
    platform.release() ("6.8.0-45-generic") yields the same key as the package
    that provides it -- that is how the running kernel is recognised.
    """
    return tuple(int(part) for part in re.findall(r"\d+", name))


def _apt_freed_bytes(output: str) -> int:
    """Bytes freed according to apt's own report, 0 when it did not say."""
    match = _APT_FREED_SPACE.search(output)
    if not match:
        return 0
    return int(float(match.group(1)) * _SI_MULTIPLIER[match.group(2)])


def _apt_removal_count(output: str) -> int:
    """How many packages apt's narration says the transaction removes.

    One line per package -- "Remv tree [2.2.1-1]", or Purg when configuration
    files go too -- printed by a real run and by `-s`/`--dry-run` alike. The
    "The following packages will be REMOVED:" block above it is not counted:
    it wraps several names onto one line and is translated.
    """
    return len(_APT_REMOVAL_LINE.findall(output))


# The `uname -r` form inside an rpm NEVRA: version-release.arch, with the epoch
# repoquery prints ("kernel-core-0:7.1.8-200.fc44.x86_64") skipped over. Anchored
# at the end and started at the first digit after a dash, so it works whether or
# not the epoch is there.
_NEVRA_VERSION = re.compile(r"-(?:\d+:)?(\d[^-]*-[^-]*)$")
# dnf's own total. dnf5 says "After this operation, 4 MiB will be freed (install
# 0 B, remove 4 MiB)"; dnf4 closes its transaction table with "Freed space: 4 M".
# Both count in 1024s, so the matched size goes straight to parse_size_from_text()
# -- what must never be handed to it is the whole transcript.
_DNF_FREED_SPACE = (
    re.compile(r"After this operation, ([0-9.]+\s*[kKMGTPE]?i?B) will be freed"),
    re.compile(r"Freed space:\s*([0-9.]+\s*[kKMGTPE]?i?B?)"),
)
# dnf's own count of what the transaction removes, from the summary it prints
# under "Transaction Summary". dnf5 says " Removing:           5 packages", dnf4
# "Remove  5 Packages"; both drop the plural at one. The number and the word are
# both required, which is what keeps the dnf5 pattern off the bare "Removing:"
# heading of the package table further up -- that line carries no count.
_DNF_REMOVAL_COUNT = (
    re.compile(r"^\s*Removing:\s+(\d+)\s+packages?\s*$", re.MULTILINE),
    re.compile(r"^\s*Remove\s+(\d+)\s+Packages?\s*$", re.MULTILINE),
)


def _rpm_kernel_version(nevra: str) -> str:
    """The kernel version an rpm package name carries, "" when it carries none.

    "kernel-core-0:7.1.8-200.fc44.x86_64" -> "7.1.8-200.fc44.x86_64", which is
    exactly what platform.release() reports for the kernel that package provides:
    the running kernel is therefore recognised by string equality, and every
    subpackage of one kernel -- kernel, kernel-core, kernel-modules... -- shares
    a single key, which is how they are kept together.
    """
    match = _NEVRA_VERSION.search(nevra)
    return match.group(1) if match else ""


def _dnf_freed_bytes(output: str) -> int:
    """Bytes freed according to dnf's own report, 0 when it did not say."""
    for pattern in _DNF_FREED_SPACE:
        match = pattern.search(output)
        if match:
            return parse_size_from_text(match.group(1))
    return 0


def _dnf_removal_count(output: str) -> int:
    """How many packages dnf's transaction summary removes, 0 when it did not say.

    The dnf-side counterpart to _apt_removal_count(), and answered from the summary
    for the same reason: dnf's per-package rows are a width-dependent table with a
    translated heading, while the summary line holds the number dnf itself arrived
    at. 0 is returned both for "removed nothing" and for a summary in a dialect
    these patterns do not know, which the caller treats alike -- there is nothing to
    report either way.
    """
    for pattern in _DNF_REMOVAL_COUNT:
        match = pattern.search(output)
        if match:
            return int(match.group(1))
    return 0


def clean_old_kernels(dry_run: bool = False) -> tuple[int, int, int]:
    """Remove old kernel packages, keeping current and one previous version."""
    current_kernel = platform.release()
    # Asked from os-release rather than from PATH, like every other
    # package-manager decision: a Fedora box with the dpkg tools installed for
    # `alien` used to take the deb branch, find no linux-image-* rows, and return
    # without ever asking dnf about its kernels.
    manager = detect_package_manager(get_os_id())
    if manager is None:
        return 0, 0, 0
    tool = resolve_admin_tool(manager)
    if not shutil.which(tool):
        return 0, 0, 0

    if manager.key == "apt" and shutil.which(manager.query_tool):
        # The matrix's query_tool, which is dpkg-query -- the tool doctor probes
        # for and the tool uninstall's scanner reads the same database with. `dpkg
        # -l` was a hand-written copy of that decision, and one that has to be
        # asked for a fixed-width table and then have the fields counted back out
        # of it.
        res = run_command(
            [manager.query_tool, "-W", f"-f={_DPKG_KERNEL_FORMAT}", "linux-image-*"],
            capture=True,
            env=C_LOCALE_ENV,
        )
        if not res.ok or not res.stdout:
            return 0, 0, 0
        running = _kernel_version_key(current_kernel)
        candidates: list[tuple[tuple[int, ...], str]] = []
        for line in res.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            status, name = parts[0].strip(), parts[1].strip()
            # Exactly what `line.startswith("ii")` used to accept, now said out
            # loud: want=install and state=installed. The want flag is the half
            # that matters here -- a kernel on hold reads "hi", and `apt-mark
            # hold` is a user saying "do not touch this package", not a size
            # estimate topo may overrule. Everything else (rc, iU, iF) is either
            # already gone or mid-transaction.
            if status[:2] != "ii":
                continue
            if not _VERSIONED_KERNEL.match(name):
                # Everything without a version in its name: the metapackages
                # (linux-image-generic on Ubuntu, linux-image-amd64 on Debian)
                # that pull in each new kernel -- purging one is how a machine
                # stops receiving kernel updates -- plus linux-image-extra-* and
                # linux-image-unsigned-*, which the orphan sweep collects once
                # the image depending on them is gone.
                continue
            key = _kernel_version_key(name)
            if key == running:
                # Never the kernel we booted from, whatever its flavour suffix.
                continue
            candidates.append((key, name))
        # Sorted by version, because dpkg lists rows alphabetically and
        # alphabetically "-100-generic" comes *before* "-91-generic": trusting
        # that order kept the oldest kernel and purged the newest one.
        candidates.sort()
        # Newest of the rest survives -- that is the "one previous version" the
        # docstring promises, and the entry a failed upgrade boots back into.
        to_remove = [pkg for _key, pkg in candidates[:-1]]
        if not to_remove:
            return 0, 0, 0
        if dry_run:
            print(f"  {SKIP} {plural(len(to_remove), 'old kernel')} would be removed")
            return 0, 0, 1
        freed = 0
        removed = 0
        for pkg in to_remove:
            purge = run_command(
                [tool, "purge", "-y", pkg],
                use_sudo=True,
                capture=True,
                env=APT_NONINTERACTIVE_ENV,
            )
            if not purge.ok:
                continue
            removed += 1
            freed += _apt_freed_bytes(purge.stdout)
        if not removed:
            return 0, 0, 0
        freed_str = f" ({bytes_to_human(freed)})" if freed else ""
        print(f"  {OK} Removed {plural(removed, 'old kernel')}{freed_str}")
        return freed, removed, 1

    elif manager.key == "dnf":
        # `--installonly` on its own. dnf5 defines it as mutually exclusive with
        # `--installed` and refuses the pair outright, so with both -- the dnf4
        # spelling this used to carry -- kernel cleaning was dead on every
        # Fedora 41+ box; alone it already means "installed" on dnf4 as well.
        # `--latest-limit=-2` is librpm's own version comparison, "all but the two
        # newest of each name.arch", so nothing here has to sort rpm versions by
        # hand (epoch, .fc44, ~rc1 and all) and the two newest of every kernel
        # subpackage are the two that stay behind. One argv token, `=` included:
        # a negative value has to be attached, or the parser reads it as an option.
        res = run_command(
            [tool, "repoquery", "--installonly", "--latest-limit=-2"],
            capture=True,
            env=C_LOCALE_ENV,
        )
        if not res.ok or not res.stdout:
            return 0, 0, 0
        # A flavour suffix is on uname's side only: the +debug kernel reports
        # "6.11.3-200.fc41.x86_64+debug" for package kernel-debug-core-6.11.3-200.
        running_version = current_kernel.partition("+")[0]
        stale: dict[str, list[str]] = {}
        for nevra in res.stdout.split():
            version = _rpm_kernel_version(nevra)
            # Never the running kernel -- dnf's protect_running_kernel would abort
            # the transaction, taking every other row in the batch down with it.
            # A row whose version cannot be read is left alone for the same
            # reason: it is not worth one refusal to remove one unnamed package.
            if not version or version == running_version:
                continue
            stale.setdefault(version, []).append(nevra)
        if not stale:
            return 0, 0, 0
        if dry_run:
            print(f"  {SKIP} {plural(len(stale), 'old kernel')} would be removed")
            return 0, 0, 1
        # Every subpackage of every stale version, in one transaction. Each
        # version used to be taken apart instead: `removable[:-1]` counted *rows*,
        # so of the five or six packages that make up one kernel it removed all
        # but one and called that leftover the fallback kernel -- a kernel-devel
        # with no image behind it, and one dnf resolved away anyway.
        packages = [nevra for nevras in stale.values() for nevra in nevras]
        remove = run_command(
            [tool, "remove", "-y", *packages], use_sudo=True, capture=True, env=C_LOCALE_ENV
        )
        if not remove.ok:
            return 0, 0, 0
        freed = _dnf_freed_bytes(remove.stdout)
        freed_str = f" ({bytes_to_human(freed)})" if freed else ""
        print(f"  {OK} Removed {plural(len(stale), 'old kernel')}{freed_str}")
        return freed, len(stale), 1

    return 0, 0, 0


def clean_rotated_logs(dry_run: bool = False) -> tuple[int, int, int]:
    """Remove rotated and compressed log files from /var/log."""
    total_size = 0
    total_items = 0
    log_dir = Path("/var/log")
    if not log_dir.exists():
        return 0, 0, 0
    rotated_suffixes = {".gz", ".xz", ".bz2", ".zst", ".old", ".1", ".2", ".3", ".4", ".5"}
    try:
        for item in log_dir.rglob("*"):
            if not item.is_file():
                continue
            if item.suffix in rotated_suffixes:
                size = get_size_fast(item)
                if dry_run:
                    total_size += size
                    total_items += 1
                else:
                    if safe_remove(item, use_trash=False)[0]:
                        total_size += size
                        total_items += 1
    except PermissionError:
        pass

    return DryRunReporter.report(
        "Rotated log files", freed_bytes=total_size, items_count=total_items, dry_run=dry_run
    )


def clean_system_data(dry_run: bool = False) -> tuple[int, int, int]:
    """Combined system and package-manager cleanup.

    The order is the one runner.py used to spell out task by task: package
    caches first, then what they leave behind, then logs, then processes.
    Kernels go before the orphan sweep: purging linux-image-X orphans its
    linux-modules-X, and in the other order those hundreds of megabytes waited
    for the *next* `topo clean` to be collected.

    Each sub-cleaner prints its own line, so aggregating here changes the
    summary rather than the transcript -- the six tasks become one row, the
    granularity the other three groups already report at.
    """
    total_size = 0
    total_items = 0
    categories = 0

    for s, i, c in (
        clean_package_manager(dry_run),
        clean_old_kernels(dry_run),
        clean_orphaned_packages(dry_run),
        clean_journal(dry_run),
        clean_rotated_logs(dry_run),
        clean_zombies(dry_run),
    ):
        total_size += s
        total_items += i
        categories += c

    return total_size, total_items, categories
