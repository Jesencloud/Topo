from pathlib import Path
from typing import Any


class ScanCache:
    """Memory-only cache for Rust engine scan results."""

    _data: dict[str, Any] = {}

    @classmethod
    def get(cls, path: Path) -> dict[str, Any] | None:
        return cls._data.get(str(path))

    @classmethod
    def set(cls, path: Path, data: dict[str, Any]) -> None:
        cls._data[str(path)] = data

    @classmethod
    def discard(cls, path: Path) -> None:
        cls._data.pop(str(path), None)

    @classmethod
    def clear(cls) -> None:
        cls._data = {}
