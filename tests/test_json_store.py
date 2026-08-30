"""The two guarantees every piece of topo's own JSON state relies on.

An interrupted write must leave the previous file intact, and a file that cannot
be parsed must be reported as *unreadable* rather than as absent -- the whitelist
turns the second distinction into a protection, since "no such file" means the
user has added nothing while "cannot read it" means their additions are the one
thing this run does not know about.
"""

import json

import pytest

from src.core.json_store import read_json, write_json_atomic


def test_read_json_separates_absent_from_unusable(tmp_path):
    assert read_json(tmp_path / "nope.json") == (None, "missing")

    good = tmp_path / "good.json"
    good.write_text(json.dumps({"a": 1}))
    assert read_json(good) == ({"a": 1}, "ok")

    truncated = tmp_path / "truncated.json"
    truncated.write_text('{"a": 1')
    assert read_json(truncated) == (None, "unreadable")

    # A directory raises IsADirectoryError, an OSError -- not a JSONDecodeError.
    assert read_json(tmp_path) == (None, "unreadable")


def test_read_json_does_not_create_what_it_reads(tmp_path):
    absent = tmp_path / "absent.json"

    read_json(absent)

    assert not absent.exists()


def test_read_json_survives_bytes_that_are_not_utf8(tmp_path):
    # json.loads() would decode these strictly and raise UnicodeDecodeError, a
    # ValueError that no caller's `except (OSError, JSONDecodeError)` catches.
    inside_a_string = tmp_path / "latin1.json"
    inside_a_string.write_bytes(b'{"note": "caf\xe9"}')
    value, state = read_json(inside_a_string)
    assert state == "ok"
    assert value["note"] == "caf�"

    not_json_at_all = tmp_path / "binary.json"
    not_json_at_all.write_bytes(b"\xff\xfe\x00\x01")
    assert read_json(not_json_at_all) == (None, "unreadable")


def test_read_json_survives_nesting_deeper_than_the_interpreter_allows(tmp_path):
    # The other non-ValueError the parser can raise: the scanner recurses per
    # level, so a file of nothing but brackets ends in RecursionError. Unreadable
    # is the honest answer; a traceback out of main() is not.
    too_deep = tmp_path / "deep.json"
    too_deep.write_text("[" * 200000 + "]" * 200000)

    assert read_json(too_deep) == (None, "unreadable")


def test_write_json_atomic_round_trips(tmp_path):
    path = tmp_path / "state.json"

    assert write_json_atomic(path, ["/keep"]) is True

    assert json.loads(path.read_text()) == ["/keep"]


def test_an_interrupted_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    write_json_atomic(path, ["/keep"])

    def dump_then_die(data, fp, **kwargs):
        fp.write('["/kee')  # a plausible amount of a half-finished dump
        raise OSError("No space left on device")

    monkeypatch.setattr(json, "dump", dump_then_die)
    assert write_json_atomic(path, ["/keep", "/also-keep"]) is False

    # The point of the sibling temp file: `open(path, "w")` would have truncated
    # this to the six bytes above, and for the whitelist that is not a lost
    # setting but a lost protection.
    assert json.loads(path.read_text()) == ["/keep"]
    assert list(tmp_path.glob("state.json.tmp-*")) == []


def test_write_json_atomic_reports_a_write_it_could_not_do(tmp_path):
    unwritable = tmp_path / "no-such-dir" / "state.json"

    assert write_json_atomic(unwritable, ["/keep"]) is False


def test_write_json_atomic_leaves_no_scratch_file_behind(tmp_path):
    path = tmp_path / "state.json"

    for value in ([], ["/a"], ["/a", "/b"]):
        assert write_json_atomic(path, value) is True

    assert sorted(p.name for p in tmp_path.iterdir()) == ["state.json"]


@pytest.mark.parametrize("value", [[], {}, ["/a"], {"config_version": 2}, 0, None])
def test_every_shape_topo_stores_survives_the_round_trip(tmp_path, value):
    path = tmp_path / "state.json"

    assert write_json_atomic(path, value) is True
    assert read_json(path) == (value, "ok")
