import os
import socket
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from lock_helpers import RECORD_LOCK_HOLDER, external_holder

from src.core.file_ops import (
    _AUDIT_WARNINGS_EMITTED,
    CLEANED_PATHS,
    bytes_to_human,
    clean_path_by_age,
    get_deletion_log_path,
    get_size,
    is_app_running,
    parse_size_from_text,
    parse_size_to_bytes,
    record_deletion_audit,
    register_cleaned_path,
    safe_remove,
)
from src.core.lock import is_file_locked
from src.core.whitelist import (
    add_to_whitelist,
    get_config_dir,
    get_hard_protection_reason,
    is_protected,
)


def _reset_audit_warnings() -> None:
    """The warn-once dedup set is module state shared by every test in the run."""
    _AUDIT_WARNINGS_EMITTED.clear()


def test_register_cleaned_path():
    CLEANED_PATHS.clear()
    register_cleaned_path(Path("/tmp/test_path"))
    assert "/tmp/test_path" in CLEANED_PATHS
    register_cleaned_path(None)  # Should not fail


@patch("subprocess.run")
def test_is_app_running(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    assert is_app_running("test_app") is True

    mock_run.return_value = MagicMock(returncode=1)
    assert is_app_running("test_app") is False

    mock_run.side_effect = OSError("error")
    assert is_app_running("test_app") is False


@patch("subprocess.run")
def test_is_app_running_trims_the_pattern_to_the_kernel_comm_limit(mock_run):
    """Long process names have to be cut to what the kernel actually stores.

    comm holds 15 characters plus a NUL, and pgrep -x rejects a longer pattern
    with exit 1 instead of matching the truncated name -- so guards like
    "google-chrome-stable" reported "not running" for a browser that was.
    """
    mock_run.return_value = MagicMock(returncode=0)

    assert is_app_running("google-chrome-stable") is True

    argv = mock_run.call_args.args[0]
    assert argv == ["pgrep", "-x", "google-chrome-s"]
    assert len(argv[2]) == 15

    # Names that already fit are passed through untouched.
    assert is_app_running("firefox") is True
    assert mock_run.call_args.args[0] == ["pgrep", "-x", "firefox"]


def test_whitelist_protection(test_env):
    """Verify that critical system paths are protected."""
    assert is_protected("/") is True
    assert is_protected("/usr/bin") is True
    assert is_protected("/etc/shadow") is True
    assert is_protected("/boot") is True
    assert is_protected("/run/systemd") is True
    assert is_protected("/var") is True
    assert is_protected("/var/log/journal") is True
    assert is_protected("/var/tmp") is True
    assert is_protected("/var/cache") is True
    assert is_protected("/var/tmp/topo-stale.tmp") is False
    assert is_protected("/var/cache/dnf/topo-cache") is False
    assert is_protected(test_env / "my_docs") is False


def test_safe_remove_prevents_system_deletion(test_env):
    """Verify safe_remove refuses to delete protected paths."""
    success, message = safe_remove("/", use_trash=False)
    assert success is False
    assert "whitelisted" in message.lower()


def test_safe_remove_prevents_sensitive_linux_app_data(test_env):
    profile_dir = test_env / ".mozilla/firefox/profile.default"
    profile_dir.mkdir(parents=True)
    login_db = profile_dir / "logins.json"
    login_db.write_text("{}")

    success, message = safe_remove(profile_dir, use_trash=False)

    assert success is False
    assert "whitelisted" in message.lower()
    assert login_db.exists()


def test_safe_remove_allows_browser_cache_inside_sensitive_app_data(test_env):
    cache_dirs = [
        test_env / ".config/google-chrome/Default/Cache",
        test_env / ".mozilla/firefox/profile.default/cache2",
    ]

    for cache_dir in cache_dirs:
        cache_dir.mkdir(parents=True)
        cache_file = cache_dir / "data.bin"
        cache_file.write_text("cache")

        success, message = safe_remove(cache_dir, use_trash=False)

        assert success is True
        assert "Permanently deleted" in message
        assert not cache_dir.exists()


def test_safe_remove_keeps_browser_profile_root_and_credentials(test_env):
    profile_dir = test_env / ".config/google-chrome/Default"
    profile_dir.mkdir(parents=True)
    login_db = profile_dir / "Login Data"
    login_db.write_text("{}")

    success, message = safe_remove(profile_dir, use_trash=False)

    assert success is False
    assert "whitelisted" in message.lower()
    assert login_db.exists()


def test_safe_remove_bypass_allows_app_data_cleanup(test_env):
    app_dir = test_env / ".config/discord"
    app_dir.mkdir(parents=True)
    state_file = app_dir / "Local State"
    state_file.write_text("{}")

    success, message = safe_remove(app_dir, use_trash=False, allow_app_data_removal=True)

    assert success is True
    assert "Permanently deleted" in message
    assert not app_dir.exists()


def test_safe_remove_bypass_keeps_hard_protected_credentials(test_env):
    ssh_dir = test_env / ".ssh"
    ssh_dir.mkdir()
    key_file = ssh_dir / "id_ed25519"
    key_file.write_text("secret")

    success, message = safe_remove(ssh_dir, use_trash=False, allow_app_data_removal=True)

    assert success is False
    assert message == "Path is hard-protected: credential or identity data"
    assert key_file.exists()


def test_safe_remove_bypass_respects_user_whitelist(test_env):
    protected_dir = test_env / "keep-even-on-uninstall"
    protected_dir.mkdir()
    marker = protected_dir / "data.txt"
    marker.write_text("keep")
    add_to_whitelist(str(protected_dir))

    success, message = safe_remove(protected_dir, use_trash=False, allow_app_data_removal=True)

    assert success is False
    assert message == "Path is hard-protected: user whitelist"
    assert marker.exists()


def test_safe_remove_bypass_keeps_topo_config(test_env):
    topo_config = get_config_dir()
    topo_config.mkdir(parents=True, exist_ok=True)
    marker = topo_config / "settings.json"
    marker.write_text("{}")

    success, message = safe_remove(topo_config, use_trash=False, allow_app_data_removal=True)

    assert success is False
    assert message == "Path is hard-protected: Topo configuration"
    assert marker.exists()


def test_safe_remove_self_removal_does_not_bypass_sensitive_data(test_env):
    for relative in (".ssh", ".gnupg", "Documents"):
        protected = test_env / relative
        protected.mkdir(parents=True)
        marker = protected / "secret.txt"
        marker.write_text("secret")

        success, message = safe_remove(
            protected,
            use_trash=False,
            allow_app_data_removal=True,
            allow_self_removal=True,
        )

        assert success is False
        assert "hard-protected" in message
        assert marker.exists()


def test_safe_remove_self_removal_does_not_bypass_whitelist(test_env):
    protected = test_env / "user-whitelist"
    protected.mkdir()
    add_to_whitelist(str(protected))

    success, message = safe_remove(
        protected,
        use_trash=False,
        allow_app_data_removal=True,
        allow_self_removal=True,
    )

    assert success is False
    assert message == "Path is hard-protected: user whitelist"
    assert protected.exists()


def test_safe_remove_self_removal_flag_reaches_toctou_validation(test_env):
    target = test_env / ".topo"
    target.mkdir()
    with patch(
        "src.core.file_ops.validate_path_for_deletion",
        side_effect=[(True, ""), (False, "Path is hard-protected: Topo installation")],
    ) as validate:
        success, message = safe_remove(
            target,
            use_trash=False,
            allow_app_data_removal=True,
            allow_self_removal=True,
        )

    assert success is False
    assert "TOCTOU check failed" in message
    assert validate.call_args_list[0].kwargs["allow_self_removal"] is True
    assert validate.call_args_list[1].kwargs["allow_self_removal"] is True


def test_hard_protection_reason_is_specific(test_env):
    assert get_hard_protection_reason(Path("/etc/passwd")) == "critical system path"
    assert get_hard_protection_reason(Path.home()) == "home directory"
    assert get_hard_protection_reason(test_env / ".gnupg/private.key") == (
        "credential or identity data"
    )


def test_safe_remove_deletion(test_env):
    """Verify safe_remove works for non-protected test files."""
    test_file = test_env / "temp_artifact.log"
    test_file.write_text("dummy data")

    assert test_file.exists()
    success, message = safe_remove(test_file, use_trash=False)

    assert success is True
    assert not test_file.exists()


def test_safe_remove_writes_deletion_audit(test_env, monkeypatch):
    log_path = test_env / "state" / "topo" / "deletions.log"
    monkeypatch.setenv("TOPO_DELETE_LOG", str(log_path))
    test_file = test_env / "audit.log"
    test_file.write_text("audit")

    success, message = safe_remove(test_file, use_trash=False)

    assert success is True
    assert "Permanently deleted" in message
    line = log_path.read_text().strip()
    fields = line.split("\t")
    assert fields[1:] == ["permanent", "5", "deleted", str(test_file)]


def test_audit_log_dir_and_file_permissions_are_reclaimed(test_env, monkeypatch, capsys):
    """mkdir/open modes only apply at creation, so loose legacy modes must be fixed (L-7)."""
    monkeypatch.delenv("TOPO_DELETE_LOG", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(test_env / "state"))
    log_dir = test_env / "state" / "topo"
    log_dir.mkdir(parents=True)
    log_dir.chmod(0o755)
    log_path = log_dir / "deletions.log"
    log_path.write_text("")
    log_path.chmod(0o644)

    record_deletion_audit(test_env / "x", "permanent", "deleted", 1)

    assert stat.S_IMODE(log_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600
    assert "deleted" in log_path.read_text()
    assert capsys.readouterr().err == ""


def test_audit_log_override_directory_mode_is_left_alone(test_env, monkeypatch):
    """A TOPO_DELETE_LOG directory shared with other users must not be chmod'ed.

    Under sudo the ownership gate is skipped, so tightening an arbitrary override
    directory (/tmp, /var/log) to 0700 would lock everyone else out of it.
    """
    shared = test_env / "shared"
    shared.mkdir()
    shared.chmod(0o1777)
    log_path = shared / "deletions.log"
    monkeypatch.setenv("TOPO_DELETE_LOG", str(log_path))
    monkeypatch.setattr("src.core.file_ops.os.getuid", lambda: 0)

    record_deletion_audit(test_env / "x", "permanent", "deleted", 1)

    assert stat.S_IMODE(shared.stat().st_mode) == 0o1777
    assert "deleted" in log_path.read_text()
    assert stat.S_IMODE(log_path.stat().st_mode) == 0o600


def test_audit_log_symlink_is_refused_with_a_warning(test_env, monkeypatch, capsys):
    """A symlinked log used to be dropped in complete silence (L-7)."""
    _reset_audit_warnings()
    real_target = test_env / "elsewhere.log"
    real_target.write_text("")
    log_path = test_env / "state" / "topo" / "deletions.log"
    log_path.parent.mkdir(parents=True)
    log_path.symlink_to(real_target)
    monkeypatch.setenv("TOPO_DELETE_LOG", str(log_path))

    record_deletion_audit(test_env / "x", "permanent", "deleted", 1)

    assert real_target.read_text() == ""
    err = capsys.readouterr().err
    assert "audit record dropped" in err
    assert "is a symlink" in err


def test_audit_log_owned_by_another_user_is_refused(test_env, monkeypatch, capsys):
    _reset_audit_warnings()
    log_path = test_env / "state" / "topo" / "deletions.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("")
    monkeypatch.setenv("TOPO_DELETE_LOG", str(log_path))

    real_lstat = Path.lstat
    foreign_uid = os.getuid() + 1

    def fake_lstat(self):
        st = real_lstat(self)
        if self == log_path:
            values = list(st)
            values[4] = foreign_uid
            return os.stat_result(values)
        return st

    monkeypatch.setattr(Path, "lstat", fake_lstat)
    record_deletion_audit(test_env / "x", "permanent", "deleted", 1)

    assert log_path.read_text() == ""
    assert f"owned by uid {foreign_uid}" in capsys.readouterr().err


def test_audit_log_warning_is_emitted_once_per_reason(test_env, monkeypatch, capsys):
    _reset_audit_warnings()
    log_path = test_env / "state" / "topo" / "deletions.log"
    log_path.parent.mkdir(parents=True)
    log_path.symlink_to(test_env / "elsewhere.log")
    monkeypatch.setenv("TOPO_DELETE_LOG", str(log_path))

    for _ in range(3):
        record_deletion_audit(test_env / "x", "permanent", "deleted", 1)

    assert capsys.readouterr().err.count("audit record dropped") == 1


def test_audit_log_non_regular_file_is_refused(test_env, monkeypatch, capsys):
    _reset_audit_warnings()
    log_path = test_env / "state" / "topo" / "deletions.log"
    log_path.parent.mkdir(parents=True)
    os.mkfifo(log_path)
    monkeypatch.setenv("TOPO_DELETE_LOG", str(log_path))

    record_deletion_audit(test_env / "x", "permanent", "deleted", 1)

    assert "is not a regular file" in capsys.readouterr().err


def test_safe_remove_deletes_readonly_file(test_env):
    """A 0444 file is removable: unlink() needs write permission on the parent only.

    The old occupancy gate treated "cannot open for writing" as "in use" and made
    read-only files permanently undeletable (L-4).
    """
    test_file = test_env / "readonly_artifact.log"
    test_file.write_text("immutable payload")
    test_file.chmod(0o444)

    success, message = safe_remove(test_file, use_trash=False)

    assert success is True, message
    assert not test_file.exists()


def test_safe_remove_deletes_file_held_open_by_another_process(test_env):
    """An open descriptor or a POSIX write lock must not block deletion.

    On Linux the inode outlives the name, so unlink() is safe while a writer is
    still attached; refusing here only ever produced false failures (M-6).
    """
    test_file = test_env / "held_open.log"
    test_file.write_text("busy payload")

    with external_holder(RECORD_LOCK_HOLDER, test_file):
        assert is_file_locked(test_file) is True

        success, message = safe_remove(test_file, use_trash=False)

        assert success is True, message
        assert not test_file.exists()


def test_safe_remove_dry_run_audit_keeps_file(test_env, monkeypatch):
    log_path = test_env / "state" / "topo" / "deletions.log"
    monkeypatch.setenv("TOPO_DELETE_LOG", str(log_path))
    test_file = test_env / "dry-run.log"
    test_file.write_text("preview")

    success, message = safe_remove(test_file, use_trash=False, dry_run=True)

    assert success is True
    assert message == "Dry run"
    assert test_file.exists()
    line = log_path.read_text().strip()
    fields = line.split("\t")
    assert fields[1:] == ["permanent", "7", "dry-run", str(test_file)]


def test_deletion_log_defaults_to_xdg_state_home(test_env, monkeypatch):
    state_home = test_env / "xdg-state"
    monkeypatch.delenv("TOPO_DELETE_LOG", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    assert get_deletion_log_path() == state_home / "topo" / "deletions.log"


def test_get_size_accurate(test_env):
    """Verify file size calculation."""
    test_file = test_env / "size_test.bin"
    content = b"0" * 1024  # 1KB
    test_file.write_bytes(content)

    assert get_size(test_file) == 1024

    test_dir = test_env / "size_dir"
    test_dir.mkdir()
    (test_dir / "f1").write_bytes(b"0" * 500)
    (test_dir / "f2").write_bytes(b"0" * 524)

    assert get_size(test_dir) == 1024


def test_get_size_error_handling():
    # Non-existent path
    assert get_size(Path("/tmp/this_should_never_exist_12345")) == 0

    # Mock OSError during stat AND scandir
    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.is_file", return_value=True),
        patch("pathlib.Path.stat", side_effect=OSError),
    ):
        assert get_size(Path("/tmp")) == 0

    with (
        patch("pathlib.Path.exists", return_value=True),
        patch("pathlib.Path.is_file", return_value=False),
        patch("pathlib.Path.is_symlink", return_value=False),
        patch("src.core.file_ops._get_fast_scan_data", return_value=None),
        patch("os.scandir", side_effect=OSError),
    ):
        assert get_size(Path("/tmp")) == 0


def test_bytes_to_human():
    assert bytes_to_human(500) == "500 B"
    assert bytes_to_human(1024) == "1.0 KiB"
    assert bytes_to_human(1536 * 1024) == "1.5 MiB"
    assert bytes_to_human(int(1.2 * 1024**3)) == "1.2 GiB"
    assert bytes_to_human(5 * 1024**4) == "5.0 TiB"


def test_parse_size_from_text():
    assert parse_size_from_text("freed 1.5 GB of space") == int(1.5 * 1024**3)
    assert parse_size_from_text("total 500 MB") == int(500 * 1024**2)
    assert parse_size_from_text("10 KB used") == int(10 * 1024)
    assert parse_size_to_bytes("1.5 GiB") == int(1.5 * 1024**3)
    assert parse_size_from_text("no size here") == 0
    assert parse_size_from_text("") == 0
    assert parse_size_to_bytes("4096") == 4096
    assert parse_size_to_bytes("  1024  ") == 1024
    # ...but stray numbers inside non-numeric text are not misread as bytes.
    assert parse_size_to_bytes("deleted 5 files") == 0
    # Invalid float captures (like '...' MB) safely fallback to 0 instead of crashing.
    assert parse_size_to_bytes("Need ... MB of disk space") == 0


def test_safe_remove_edge_cases(test_env):
    # Test non-existent file
    success, msg = safe_remove(test_env / "non_existent.txt")
    assert success is False
    assert "not exist" in msg

    # Test critical paths fallback protection
    with patch("src.core.file_ops.is_protected", return_value=False):
        success, msg = safe_remove(Path("/"))
        assert success is False
        assert "critical system path" in msg.lower()

    # Test that trash failure does NOT silently fall through to permanent delete
    test_file = test_env / "trash_test.txt"
    test_file.write_text("dummy")
    log_path = test_env / "state" / "topo" / "deletions.log"
    with (
        patch.dict("os.environ", {"TOPO_DELETE_LOG": str(log_path)}),
        patch("shutil.which", return_value=True),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=1)  # Trash command fails
        success, msg = safe_remove(test_file, use_trash=True)
        assert success is False
        assert "refusing" in msg.lower()
    assert test_file.exists(), "File must survive when trash fails"
    lines = log_path.read_text().splitlines()
    assert lines[0].split("\t")[1:] == ["trash", "5", "trash-failed", str(test_file)]

    # Test Exception handling during removal
    with patch("pathlib.Path.unlink", side_effect=OSError("mocked error")):
        test_file = test_env / "err_test.txt"
        test_file.write_text("dummy")
        success, msg = safe_remove(test_file, use_trash=False)
        assert success is False
        assert "mocked error" in msg


def test_safe_remove_deletes_symlink_not_target(test_env):
    target_dir = test_env / "target"
    target_dir.mkdir()
    target_file = target_dir / "kept.txt"
    target_file.write_text("keep")
    link = test_env / "target-link"
    link.symlink_to(target_dir, target_is_directory=True)

    success, msg = safe_remove(link, use_trash=False)

    assert success is True
    assert "Permanently deleted" in msg
    assert not link.exists()
    assert target_dir.exists()
    assert target_file.exists()


def test_safe_remove_deletes_broken_symlink(test_env):
    link = test_env / "broken-link"
    link.symlink_to(test_env / "missing-target")

    success, msg = safe_remove(link, use_trash=False)

    assert success is True
    assert "Permanently deleted" in msg
    assert not link.is_symlink()


def test_safe_remove_respects_parent_whitelist(test_env):
    parent = test_env / "protected"
    parent.mkdir()
    child = parent / "child.txt"
    child.write_text("keep")

    with patch("src.core.file_ops.is_protected", return_value=True):
        success, msg = safe_remove(child, use_trash=False)

    assert success is False
    assert "whitelisted" in msg
    assert child.exists()


def test_safe_remove_reports_permission_error(test_env):
    test_file = test_env / "readonly.txt"
    test_file.write_text("data")

    with patch("pathlib.Path.unlink", side_effect=PermissionError("denied")):
        success, msg = safe_remove(test_file, use_trash=False)

    assert success is False
    assert "denied" in msg


def test_clean_path_by_age_leaves_sockets_and_fifos_in_place(test_env):
    """The age pruner skips entry types that hold no reclaimable bytes.

    A bound socket's timestamps never move, so age alone always eventually
    classifies it as stale -- and deleting it costs a running process its
    endpoint while freeing nothing.
    """
    cache_dir = test_env / "mixed"
    cache_dir.mkdir()
    stale_file = cache_dir / "payload.bin"
    stale_file.write_bytes(b"junk")
    fifo = cache_dir / "pipe"
    os.mkfifo(fifo)
    sock_path = cache_dir / "agent.4321"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sock_path))

    old_time = time.time() - 20 * 86400
    for target in (stale_file, fifo, sock_path):
        os.utime(target, (old_time, old_time), follow_symlinks=False)

    try:
        size, items = clean_path_by_age(cache_dir, days=10)
    finally:
        sock.close()

    assert (size, items) == (4, 1)
    assert not stale_file.exists()
    assert fifo.exists()
    assert sock_path.exists()


def test_clean_path_by_age_keeps_a_directory_holding_a_live_socket(test_env):
    """Skipping the socket is only half a guard if its directory still goes.

    Removing a directory takes its whole tree with it, so an ssh-agent directory
    -- whose one socket has timestamps that never move -- has to be kept as well.
    """
    agent_dir = test_env / "ssh-XXXXaBcDeF"
    agent_dir.mkdir()
    sock_path = agent_dir / "agent.4321"
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(sock_path))
    junk_dir = test_env / "stale-build"
    junk_dir.mkdir()
    (junk_dir / "payload.bin").write_bytes(b"junk")

    old_time = time.time() - 20 * 86400
    for target in (sock_path, agent_dir, junk_dir / "payload.bin", junk_dir):
        os.utime(target, (old_time, old_time), follow_symlinks=False)

    try:
        size, items = clean_path_by_age(test_env, days=10)
    finally:
        sock.close()

    assert (size, items) == (4, 1)
    assert sock_path.exists()
    assert not junk_dir.exists()


def test_clean_path_by_age(test_env):
    cache_dir = test_env / "cache"
    cache_dir.mkdir()
    f1 = cache_dir / "old_file.txt"
    f2 = cache_dir / "new_file.txt"
    f1.write_text("old")
    f2.write_text("new")

    current_time = time.time()
    old_time = current_time - (15 * 86400)

    # Use a real os.stat_result to avoid TypeError on some platforms/Python versions

    mock_st = os.stat_result((0, 0, 0, 0, 0, 0, 10, old_time, old_time, old_time))

    # We mock the DirEntry.stat used to judge entry age
    with patch("os.DirEntry.stat", return_value=mock_st):
        # Both files look old by atime AND mtime

        # Dry run
        size, items = clean_path_by_age(cache_dir, days=10, dry_run=True)
        assert items == 2

        # Real run
        with patch("pathlib.Path.unlink") as mock_unlink:
            size, items = clean_path_by_age(cache_dir, days=10, dry_run=False)
            assert items == 2
            assert mock_unlink.call_count == 2

    # Test OSError handling
    with patch("os.scandir", side_effect=OSError):
        size, items = clean_path_by_age(cache_dir, days=10)
        assert size == 0
        assert items == 0


def test_clean_path_by_age_uses_single_stats_scan_for_old_directory(test_env):
    cache_dir = test_env / "cache-stats"
    old_dir = cache_dir / "old"
    old_dir.mkdir(parents=True)
    (old_dir / "payload").write_bytes(b"data")
    old_time = time.time() - 20 * 86400
    os.utime(old_dir, (old_time, old_time))

    with (
        patch(
            "src.core.file_ops._get_path_stats",
            return_value={"total_size_bytes": 4, "newest_activity_secs": old_time},
        ) as stats,
        patch("src.core.file_ops._has_recent_content") as recent,
        patch("src.core.file_ops.get_size_fast") as size_scan,
        patch("src.core.file_ops.safe_remove", return_value=(True, "ok")) as remove,
    ):
        size, count = clean_path_by_age(cache_dir, days=10)

    assert (size, count) == (4, 1)
    stats.assert_called_once_with(old_dir)
    recent.assert_not_called()
    size_scan.assert_not_called()
    assert remove.call_args.kwargs["known_size_bytes"] == 4


def test_record_deletion_audit_escapes_control_chars(test_env):
    """Regression (L1/N-4): a rejected path carrying newlines, tabs, ANSI escapes,
    Unicode line separators or BiDi overrides must not forge extra audit records,
    shift the tab-separated column layout, or reach the log raw."""
    from src.core.history import parse_deletion_history

    log_path = test_env / "state" / "topo" / "deletions.log"
    with patch.dict("os.environ", {"TOPO_DELETE_LOG": str(log_path)}):
        record_deletion_audit(
            "/tmp/evil\x1b[2K\nFORGED\trow\N{LINE SEPARATOR}LS\N{RIGHT-TO-LEFT OVERRIDE}RLO",
            "permanent",
            "rejected-validation",
        )

    content = log_path.read_text()
    # The embedded newline is escaped, so the file holds exactly one record line.
    assert content.count("\n") == 1
    assert "\\x1b[2K\\x0aFORGED\\x09row\\u2028LS\\u202eRLO" in content
    # U+2028 is a line break for str.splitlines(), and U+202E flips the rendered
    # order of the rest of the line, so neither may survive raw in the log.
    assert "\N{LINE SEPARATOR}" not in content
    assert "\N{RIGHT-TO-LEFT OVERRIDE}" not in content
    assert len(content.splitlines()) == 1
    # The parser sees a single event, never a forged second one.
    sessions = parse_deletion_history(log_path)
    assert sum(len(s.events) for s in sessions) == 1


def test_history_refuses_to_read_a_symlinked_audit_log(test_env, capsys):
    """Rendering a log somebody else can redirect would show fabricated history (L-7)."""
    from src.core.history import parse_deletion_history

    _reset_audit_warnings()
    planted = test_env / "planted.log"
    planted.write_text("2026-08-12T00:00:00+08:00\tpermanent\t1\tdeleted\t/etc/passwd\n")
    log_path = test_env / "state" / "topo" / "deletions.log"
    log_path.parent.mkdir(parents=True)
    log_path.symlink_to(planted)

    assert parse_deletion_history(log_path) == []
    assert "not reading history from it" in capsys.readouterr().err


def test_history_reads_a_regular_owned_audit_log(test_env):
    from src.core.history import parse_deletion_history

    log_path = test_env / "state" / "topo" / "deletions.log"
    with patch.dict("os.environ", {"TOPO_DELETE_LOG": str(log_path)}):
        record_deletion_audit(test_env / "gone", "permanent", "deleted", 7)

    sessions = parse_deletion_history(log_path)
    assert sum(len(s.events) for s in sessions) == 1
