"""The one rule for reading what a cleaner returned.

A sub-cleaner's return is deliberately ragged: the category count is inferred
when a cleaner has nothing to say about it, and `trashed_bytes` defaults to zero
because most cleaners only ever unlink. Three call sites used to spell that
convention out themselves, so the default for a missing tail was written three
times and could disagree in three places. It is written once here.
"""

from collections.abc import Sequence


def as_totals(result: Sequence[int]) -> tuple[int, int, int, int]:
    """Normalise a cleaner's return into (size, items, categories, trashed_bytes)."""
    size, items = result[0], result[1]
    categories = result[2] if len(result) >= 3 else (1 if items > 0 else 0)
    trashed_bytes = result[3] if len(result) >= 4 else 0
    return size, items, categories, trashed_bytes
