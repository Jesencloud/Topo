import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.clean.runner import (
    CleanupTask,
    _print_cleanup_summary,
    build_execution_groups,
    run_clean,
)

CLEAN_PACKAGE = Path(__file__).resolve().parent.parent / "src" / "clean"

# `glyph, status = (SKIP, "would be cleaned") if dry_run else (OK, "cleaned")`
_GLYPH_PHRASE_PAIR = re.compile(r'\((OK|SKIP), "([^"]*)"\)')


def test_no_preview_line_wears_the_glyph_of_a_finished_delete():
    """Every dry-run line in clean/ carries SKIP, and no `would` line carries OK.

    `✓ npm cache (40 MB) would be cleaned` differed from the real delete's
    `✓ npm cache (40 MB) cleaned` by a verb tense alone -- the one difference a
    reader skimming a 40-line report does not catch, and the one that survives
    neither `--no-color` nor a paste into an issue. There are 20-odd such lines
    across four modules and each was written by hand, so the rule is checked
    over the source: a new cleaner copied from an old one is exactly how the
    green `✓` would come back.
    """
    offenders = []
    checked = 0
    for module in sorted(CLEAN_PACKAGE.glob("*.py")):
        for lineno, line in enumerate(module.read_text(encoding="utf-8").splitlines(), 1):
            where = f"{module.name}:{lineno}"
            if "{OK}" in line and "would" in line:
                offenders.append(where)
            for glyph, phrase in _GLYPH_PHRASE_PAIR.findall(line):
                # The tense and the glyph are chosen as one tuple precisely so
                # they cannot drift apart; this is that pairing, asserted.
                checked += 1
                if phrase.startswith("would") != (glyph == "SKIP"):
                    offenders.append(f"{where} ({glyph} paired with {phrase!r})")
            if "{SKIP}" in line:
                checked += 1
    assert offenders == []
    # A sweep that matched nothing would pass just as quietly.
    assert checked > 20


def test_build_execution_groups_contains_all_cleanup_categories():
    groups = build_execution_groups({"demo": {}})

    assert [header.split("➤ ", 1)[1].split("\x1b", 1)[0] for header, _ in groups] == [
        "System & Package Manager",
        "User Data Cleanup",
        "Deep App Cleanup",
        "Developer Tools & AI Models",
    ]
    assert [[task.name for task in tasks] for _, tasks in groups] == [
        ["System & Packages"],
        ["User Data & Trash"],
        ["Deep App Caches"],
        ["Developer Artifacts"],
    ]


def test_print_cleanup_summary_dry_run_reports_breakdown(capsys):
    with patch(
        "src.clean.runner.shutil.disk_usage",
        return_value=SimpleNamespace(free=10 * 1024**3),
    ) as disk_usage:
        _print_cleanup_summary(True, 2048, 3, [("Cache", 2048, 3)])

    disk_usage.assert_called_once()
    output = capsys.readouterr().out
    assert "Scan complete (Preview)" in output
    assert "Total space that can be freed" in output
    assert "Cache" in output
    assert "Run without --dry-run" in output
    assert "Free space now" not in output


def test_print_cleanup_summary_actual_cleanup_reports_free_space(capsys):
    with patch(
        "src.clean.runner.shutil.disk_usage",
        return_value=SimpleNamespace(free=10 * 1024**3),
    ):
        _print_cleanup_summary(False, 2 * 1024**3, 1, [("Cache", 2 * 1024**3, 1)])

    output = capsys.readouterr().out
    assert "Cleanup complete" in output
    assert "Total space freed" in output
    assert "Equivalent to ~0.2 4K movies" in output
    assert "Free space now" in output


def test_print_cleanup_summary_aligns_breakdown_item_counts(capsys):
    with patch(
        "src.clean.runner.shutil.disk_usage",
        return_value=SimpleNamespace(free=10 * 1024**3),
    ):
        _print_cleanup_summary(
            False,
            1,
            23,
            [
                ("System & Packages", 1, 1),
                ("User Data & Trash", 1, 2),
                ("Deep App Caches", 1, 18),
            ],
        )

    breakdown = [line for line in capsys.readouterr().out.splitlines() if line.startswith("  •")]
    assert [line[line.index("(") :] for line in breakdown] == [
        "( 1 item )",
        "( 2 items)",
        "(18 items)",
    ]
    assert len({line.index(")") for line in breakdown}) == 1


def test_print_cleanup_summary_reserves_count_column_for_single_digits(capsys):
    with patch(
        "src.clean.runner.shutil.disk_usage",
        return_value=SimpleNamespace(free=10 * 1024**3),
    ):
        _print_cleanup_summary(False, 1, 3, [("Cache", 1, 1), ("Logs", 1, 2)])

    breakdown = [line for line in capsys.readouterr().out.splitlines() if line.startswith("  •")]
    assert [line[line.index("(") :] for line in breakdown] == [
        "( 1 item )",
        "( 2 items)",
    ]
    assert len({line.index(")") for line in breakdown}) == 1


def test_run_clean_returns_false_when_sudo_authentication_fails():
    with (
        patch("src.clean.runner.proactive_app_detection", return_value={}),
        patch("src.clean.runner.system.authenticate_sudo_session", return_value=False) as auth,
        patch("src.clean.runner.record_history_session") as history,
    ):
        assert run_clean(dry_run=True) is False

    auth.assert_called_once_with(True, request_subject="System caches", action="cleanup")
    history.assert_not_called()


def test_run_clean_executes_tasks_records_history_and_clears_scan_cache(capsys):
    calls = []

    def task_with_output(dry_run=False):
        calls.append(dry_run)
        print("cleaned cache")
        return 2 * 1024**3, 2, 1

    def empty_task(dry_run=False):
        return 0, 0, 0

    groups = [
        ("Category", [CleanupTask("Cache", task_with_output), CleanupTask("Empty", empty_task)])
    ]
    with (
        patch("src.clean.runner.proactive_app_detection", return_value={}),
        patch("src.clean.runner.system.authenticate_sudo_session", return_value=True),
        patch("src.clean.runner.build_execution_groups", return_value=groups),
        patch("src.clean.runner.record_history_session") as history,
        patch("src.clean.runner.ScanCache.clear") as clear_cache,
        patch(
            "src.clean.runner.shutil.disk_usage",
            return_value=SimpleNamespace(free=10 * 1024**3),
        ),
    ):
        assert run_clean() is True

    assert calls == [False]
    assert history.call_args_list[0].args == ("clean", "started")
    assert history.call_args_list[1].args == ("clean", "ended")
    clear_cache.assert_called_once_with()
    output = capsys.readouterr().out
    assert "Category" in output
    assert "cleaned cache" in output
    assert "Cleanup complete" in output


def test_run_clean_reports_what_it_deleted_before_a_ctrl_c(capsys):
    """Ctrl-C mid-run keeps the record of everything already deleted.

    The group's own `✓` lines were still buffered inside redirect_stdout, the
    summary never ran and no closing line reached the audit log, so a run that
    deleted 2 GB left one line of "interrupted" behind and showed up in
    `topo history` as `incomplete` forever.
    """

    def deleting_task(dry_run=False):
        print("cleaned cache")
        return 2 * 1024**3, 2, 1

    def interrupted_task(dry_run=False):
        print("half of this group")
        raise KeyboardInterrupt

    groups = [
        ("First Category", [CleanupTask("Cache", deleting_task)]),
        ("Second Category", [CleanupTask("Interrupted", interrupted_task)]),
    ]
    with (
        patch("src.clean.runner.proactive_app_detection", return_value={}),
        patch("src.clean.runner.system.authenticate_sudo_session", return_value=True),
        patch("src.clean.runner.build_execution_groups", return_value=groups),
        patch("src.clean.runner.record_history_session") as history,
        patch("src.clean.runner.ScanCache.clear") as clear_cache,
        patch(
            "src.clean.runner.shutil.disk_usage",
            return_value=SimpleNamespace(free=10 * 1024**3),
        ),
        pytest.raises(KeyboardInterrupt),
    ):
        run_clean()

    # The interrupt still propagates -- main() turns it into exit 130 -- but the
    # report, the cache invalidation and the closing history line all happened
    # on the way out.
    assert history.call_args_list[-1].args == ("clean", "interrupted")
    clear_cache.assert_called_once_with()
    output = capsys.readouterr().out
    assert "cleaned cache" in output
    # Even the partial group flushes what it managed to print.
    assert "half of this group" in output
    assert "Cleanup interrupted" in output
    assert "before the interrupt" in output
    assert "the rest never started" in output


def test_run_clean_does_not_log_a_finish_when_the_run_was_killed(capsys):
    """Only a run that got through every group may report as finished.

    The report moved into a `finally`, which by itself would have signed off any
    exit as `ended` -- including SIGTERM, which arrives as SystemExit rather than
    KeyboardInterrupt, and a task raising outright. Both would have written a
    completion into the audit log for a run that never completed.
    """

    def killed_task(dry_run=False):
        raise SystemExit(143)

    groups = [("Category", [CleanupTask("Cache", killed_task)])]
    with (
        patch("src.clean.runner.proactive_app_detection", return_value={}),
        patch("src.clean.runner.system.authenticate_sudo_session", return_value=True),
        patch("src.clean.runner.build_execution_groups", return_value=groups),
        patch("src.clean.runner.record_history_session") as history,
        patch(
            "src.clean.runner.shutil.disk_usage",
            return_value=SimpleNamespace(free=10 * 1024**3),
        ),
        pytest.raises(SystemExit),
    ):
        run_clean()

    assert history.call_args_list[-1].args == ("clean", "interrupted")
    assert "Cleanup interrupted" in capsys.readouterr().out
