"""Tests for src/core/render.py.

Only bytes_to_human is exercised here. The bar and percentage helpers next to it
in that module are reached through ui.navigator's re-export instead -- 26 uses in
tests/test_navigator.py and one in tests/test_no_color.py -- because that is
where their callers are, and where a broken draw_bar shows up as a broken row
rather than as a wrong string.
"""

from src.core.render import bytes_to_human


def test_bytes_to_human():
    assert bytes_to_human(500) == "500 B"
    assert bytes_to_human(1024) == "1.0 KiB"
    assert bytes_to_human(1536 * 1024) == "1.5 MiB"
    assert bytes_to_human(int(1.2 * 1024**3)) == "1.2 GiB"
    assert bytes_to_human(5 * 1024**4) == "5.0 TiB"
