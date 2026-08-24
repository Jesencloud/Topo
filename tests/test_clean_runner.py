from types import SimpleNamespace
from unittest.mock import patch

from src.clean.runner import CleanupTask, TaskRegistry, _print_cleanup_summary, run_clean


def test_build_execution_groups_contains_all_cleanup_categories():
    groups = TaskRegistry.build_execution_groups({"demo": {}})

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
        patch("src.clean.runner.TaskRegistry.build_execution_groups", return_value=groups),
        patch("src.clean.runner.record_history_session") as history,
        patch("src.clean.runner.ScanCache.clear") as clear_cache,
        patch(
            "src.clean.runner.shutil.disk_usage",
            return_value=SimpleNamespace(free=10 * 1024**3),
        ),
    ):
        assert run_clean() is None

    assert calls == [False]
    assert history.call_args_list[0].args == ("clean", "started")
    assert history.call_args_list[1].args == ("clean", "ended")
    clear_cache.assert_called_once_with()
    output = capsys.readouterr().out
    assert "Category" in output
    assert "cleaned cache" in output
    assert "Cleanup complete" in output
