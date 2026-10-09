"""One place decides how wide topo's I/O fan-out gets, and the user can cap it.

Five call sites used to carry five unrelated numbers, one of them "a thread per
task" with no ceiling, and nothing asked the machine how many CPUs it had.
"""

from concurrent.futures import ThreadPoolExecutor

import pytest

from src.core.concurrency import (
    MAX_WORKERS_ENV,
    PROBE_WORKER_CAP,
    SCAN_THREADS_ENV,
    SCAN_WORKER_CAP,
    engine_env,
    worker_ceiling,
    workers,
)


def test_a_pool_is_never_wider_than_its_work(monkeypatch):
    monkeypatch.delenv(MAX_WORKERS_ENV, raising=False)
    monkeypatch.setattr("src.core.concurrency.os.cpu_count", lambda: 64)

    assert workers(1, cap=PROBE_WORKER_CAP) == 1
    assert workers(3, cap=PROBE_WORKER_CAP) == 3
    assert workers(100, cap=PROBE_WORKER_CAP) == PROBE_WORKER_CAP


def test_a_pool_always_has_at_least_one_thread(monkeypatch):
    # ThreadPoolExecutor rejects 0, and an empty batch still has to be legal to
    # ask about -- discovery.py used to pass len(tasks) straight through.
    monkeypatch.delenv(MAX_WORKERS_ENV, raising=False)
    for count in (0, -5):
        assert workers(count, cap=PROBE_WORKER_CAP) == 1
        ThreadPoolExecutor(max_workers=workers(count, cap=PROBE_WORKER_CAP)).shutdown()


def test_a_small_machine_narrows_every_pool(monkeypatch):
    # The defect: a single-core container ran the same fan-out as a 128-core host.
    monkeypatch.delenv(MAX_WORKERS_ENV, raising=False)
    monkeypatch.setattr("src.core.concurrency.os.cpu_count", lambda: 1)

    assert workers(100, cap=PROBE_WORKER_CAP) == 1
    assert workers(100, cap=SCAN_WORKER_CAP) == 1


def test_the_scan_cap_stays_narrow_on_a_big_machine(monkeypatch):
    # Each scan thread forks an engine that parallelises across every core, so
    # the Python-side width is deliberately not scaled up with the core count.
    monkeypatch.delenv(MAX_WORKERS_ENV, raising=False)
    monkeypatch.setattr("src.core.concurrency.os.cpu_count", lambda: 128)

    assert workers(100, cap=SCAN_WORKER_CAP) == SCAN_WORKER_CAP


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1", 1), ("4", 4), ("0", 1), ("-3", 1)],
)
def test_the_user_ceiling_caps_every_pool(monkeypatch, value, expected):
    # The knob the review asked for: someone whose $HOME is on NFS pins this to 1
    # and every fan-out in the program goes serial.
    monkeypatch.setenv(MAX_WORKERS_ENV, value)
    monkeypatch.setattr("src.core.concurrency.os.cpu_count", lambda: 64)

    assert worker_ceiling() == expected
    assert workers(100, cap=PROBE_WORKER_CAP) == expected


@pytest.mark.parametrize("value", ["", "   ", "lots", "3.5"])
def test_an_unusable_ceiling_is_ignored_rather_than_fatal(monkeypatch, value):
    # Failing a cleanup over a typo in an environment variable would be worse
    # than running at the default width.
    monkeypatch.setenv(MAX_WORKERS_ENV, value)
    monkeypatch.setattr("src.core.concurrency.os.cpu_count", lambda: 64)

    assert worker_ceiling() is None
    assert workers(100, cap=PROBE_WORKER_CAP) == PROBE_WORKER_CAP


def test_an_unknown_cpu_count_does_not_crash_the_pool(monkeypatch):
    # os.cpu_count() is documented to be able to return None.
    monkeypatch.delenv(MAX_WORKERS_ENV, raising=False)
    monkeypatch.setattr("src.core.concurrency.os.cpu_count", lambda: None)

    assert workers(100, cap=PROBE_WORKER_CAP) == 1


def test_the_engine_inherits_an_explicit_scan_thread_setting_untouched(monkeypatch):
    # Being explicit about the engine beats anything inferred: the child already
    # inherits os.environ, so the overlay has nothing to add.
    monkeypatch.setenv(SCAN_THREADS_ENV, "6")
    monkeypatch.setenv(MAX_WORKERS_ENV, "1")

    assert engine_env() == {}


def test_the_io_ceiling_reaches_the_engine(monkeypatch):
    # Pinning topo to 1 because $HOME is on NFS has to mean the engine too --
    # that is where most of the concurrency lives.
    monkeypatch.delenv(SCAN_THREADS_ENV, raising=False)
    monkeypatch.setenv(MAX_WORKERS_ENV, "2")

    assert engine_env() == {SCAN_THREADS_ENV: "2"}


def test_no_knobs_set_leaves_the_engine_default_alone(monkeypatch):
    # Which width wins on a given disk is a question for a benchmark, so no
    # default is invented here.
    monkeypatch.delenv(SCAN_THREADS_ENV, raising=False)
    monkeypatch.delenv(MAX_WORKERS_ENV, raising=False)

    assert engine_env() == {}
