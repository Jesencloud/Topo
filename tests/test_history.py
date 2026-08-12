from src.core.history import parse_deletion_history, render_history


def test_parse_session_history(tmp_path):
    log = tmp_path / "deletions.log"
    log.write_text(
        "\n".join(
            [
                "2026-05-31T10:00:00+08:00\tsession\t0\tstarted\tclean",
                "2026-05-31T10:00:01+08:00\tpermanent\t1024\tdeleted\t/tmp/a",
                "2026-05-31T10:00:01+08:00\tdnf\t4096\tremoved\theavy-app",
                "2026-05-31T10:00:02+08:00\ttrash\t2048\ttrashed-gio\t/tmp/b",
                "2026-05-31T10:00:03+08:00\tpermanent\tunknown\tfailed\t/tmp/c",
                "2026-05-31T10:00:04+08:00\tsession\t0\tended\tclean",
            ]
        )
    )

    sessions = parse_deletion_history(log)

    assert len(sessions) == 1
    session = sessions[0]
    assert session.command == "clean"
    assert session.removed == 2
    assert session.trashed == 1
    assert session.failed == 1
    assert session.skipped == 0
    assert session.total_size == 7168


def test_parse_legacy_ungrouped_history(tmp_path):
    log = tmp_path / "deletions.log"
    log.write_text(
        "\n".join(
            [
                "2026-05-31T11:00:01+08:00\tpermanent\t100\tdeleted\t/tmp/a",
                "2026-05-31T11:00:02+08:00\tpermanent\t0\trejected-validation\t/etc/passwd",
            ]
        )
    )

    sessions = parse_deletion_history(log)

    assert len(sessions) == 1
    assert sessions[0].command == "legacy"
    assert sessions[0].removed == 1
    assert sessions[0].skipped == 1


def test_every_rejected_status_counts_as_skipped(tmp_path):
    """A new rejection reason must be tallied the day it is introduced.

    analyze._sudo_remove and file_ops.safe_remove each emit their own
    ``rejected-*`` status; enumerating them one by one in SKIPPED_STATUSES let
    rejected-toctou and the ancestor checks silently fall out of every count.
    They are matched by the ``rejected-`` prefix instead.
    """
    statuses = [
        "rejected-validation",
        "rejected-toctou",
        "rejected-unreadable-ancestor",
        "rejected-ancestor-symlink",
        "rejected-unsafe-ancestor",
    ]
    log = tmp_path / "deletions.log"
    log.write_text(
        "\n".join(
            f"2026-05-31T12:00:0{i}+08:00\tsudo-permanent\t0\t{status}\t/etc/target{i}"
            for i, status in enumerate(statuses)
        )
    )

    sessions = parse_deletion_history(log)

    assert len(sessions) == 1
    assert sessions[0].skipped == len(statuses)
    assert sessions[0].failed == 0


def test_render_history_summary(tmp_path):
    log = tmp_path / "deletions.log"
    log.write_text(
        "\n".join(
            [
                "2026-05-31T10:00:00+08:00\tsession\t0\tstarted\tclean",
                "2026-05-31T10:00:01+08:00\tpermanent\t1024\tdeleted\t/tmp/a",
                "2026-05-31T10:00:02+08:00\tsession\t0\tended\tclean",
            ]
        )
    )

    output = render_history(parse_deletion_history(log))

    assert "Deletion History" in output
    assert "removed=1" in output
    assert "size=1.0 KiB" in output
