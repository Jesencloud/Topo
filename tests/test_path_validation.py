import os
from pathlib import Path
from unittest.mock import patch

from src.core.file_ops import validate_path_for_deletion
from src.core.whitelist import SYSTEM_CLEANABLE_ROOTS, is_system_cleanable_content

CORPUS = Path(__file__).parent / "fuzz_corpus" / "dangerous_paths.txt"


def _dangerous_paths() -> list[str]:
    paths = []
    for line in CORPUS.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            paths.append(line)
    return paths


def test_dangerous_path_corpus_is_rejected(test_env):
    accepted = []
    for path in _dangerous_paths():
        ok, _ = validate_path_for_deletion(path)
        if ok:
            accepted.append(path)

    assert not accepted
    assert len(_dangerous_paths()) >= 40


def test_control_character_paths_are_rejected(test_env):
    for path in ["/tmp/with\nnewline", "/tmp/with\ttab", "/tmp/with\x00nul"]:
        ok, reason = validate_path_for_deletion(path)
        assert ok is False
        assert "control" in reason


def test_user_owned_absolute_noncritical_path_is_allowed(test_env):
    path = test_env / "cache" / "item"
    ok, reason = validate_path_for_deletion(path)

    assert ok is True
    assert reason == ""


def test_system_cache_and_temp_contents_are_allowed_but_roots_are_rejected():
    for root in ["/var/tmp", "/var/cache"]:
        ok, reason = validate_path_for_deletion(root)
        assert ok is False
        assert reason in {"Path is whitelisted", "Refusing to delete critical system path"}

    for child in ["/var/tmp/topo-stale.tmp", "/var/cache/dnf/topo-cache"]:
        ok, reason = validate_path_for_deletion(child)
        assert ok is True
        assert reason == ""


def test_non_allowlisted_var_cache_content_stays_protected():
    """The carve-out is an allowlist of package caches, not all of /var/cache (M-3)."""
    for path in [
        "/var/cache/ldconfig/aux-cache",
        "/var/cache/private/secret",
        "/var/cache/unattended-upgrades/log",
    ]:
        ok, reason = validate_path_for_deletion(path)
        assert ok is False, path
        assert reason in {"Path is whitelisted", "Refusing to delete critical system path"}


def test_allowlisted_package_cache_content_is_still_cleanable():
    for path in [
        "/var/cache/apt/archives/foo.deb",
        "/var/cache/pacman/pkg/foo.pkg.tar.zst",
        "/var/cache/libdnf5/repo",
    ]:
        ok, reason = validate_path_for_deletion(path)
        assert ok is True, path
        assert reason == ""


def test_package_manager_container_directories_are_protected():
    """apt refuses to run at all once archives/partial is gone and never
    recreates it, so the directory itself is off limits while its contents and
    its siblings stay cleanable."""
    ok, reason = validate_path_for_deletion("/var/cache/apt/archives/partial")
    assert ok is False
    assert reason in {"Path is whitelisted", "Refusing to delete critical system path"}
    assert is_system_cleanable_content(Path("/var/cache/apt/archives/partial")) is False

    for path in ["/var/cache/apt/archives/partial/half.deb", "/var/cache/apt/archives/foo.deb"]:
        ok, reason = validate_path_for_deletion(path)
        assert ok is True, path
        assert reason == ""


def test_var_tmp_content_owned_by_another_user_is_protected(monkeypatch):
    """Only the current user's own /var/tmp entries are cleanable (M-3)."""
    entry = Path("/var/tmp") / "topo-owned-by-someone-else"  # nosec B108 - test path only
    fake_stat = os.stat_result((0o100644, 1, 1, 1, os.getuid() + 1, 0, 0, 0, 0, 0))
    monkeypatch.setattr(Path, "lstat", lambda self: fake_stat)

    assert is_system_cleanable_content(entry) is False


def test_var_tmp_content_owned_by_current_user_is_cleanable():
    entry = Path("/var/tmp") / "topo-mine"  # nosec B108 - test path only
    fake_stat = os.stat_result((0o100644, 1, 1, 1, os.getuid(), 0, 0, 0, 0, 0))
    with patch.object(Path, "lstat", lambda self: fake_stat):
        assert is_system_cleanable_content(entry) is True


def test_system_cleanable_roots_themselves_are_never_content():
    for root in SYSTEM_CLEANABLE_ROOTS:
        assert is_system_cleanable_content(root) is False


def test_redundant_prefix_guard_still_blocks_system_children():
    for path in ["/etc/passwd", "/usr/bin/bash", "/var/log/journal"]:
        ok, reason = validate_path_for_deletion(path)
        assert ok is False
        assert reason in {"Path is whitelisted", "Refusing to delete critical system path"}


def test_symlink_to_critical_path_is_rejected(test_env):
    link = test_env / "passwd-link"
    link.symlink_to("/etc/passwd")

    ok, reason = validate_path_for_deletion(link)

    assert ok is False
    assert reason in {"Path is whitelisted", "Refusing to delete critical system path"}


def test_broken_symlink_under_user_path_is_allowed(test_env):
    link = test_env / "broken-link"
    link.symlink_to(test_env / "missing-target")

    ok, reason = validate_path_for_deletion(link)

    assert ok is True
    assert reason == ""
