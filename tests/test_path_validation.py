from pathlib import Path

from src.core.file_ops import validate_path_for_deletion

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
