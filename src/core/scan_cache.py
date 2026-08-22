import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class _CacheEntry:
    data: dict[str, Any]
    estimated_bytes: int
    signature: tuple[int, int, int] | None


class ScanCache:
    """Bounded, freshness-aware memory cache for Rust scan results.

    Thread-safe: ``get``/``set``/``discard``/``clear`` are guarded by a
    class-level lock. ``analyze.parallel_scan_sizes`` seeds the cache from a
    ThreadPoolExecutor (several workers each calling ``set`` many times), and
    the byte accounting (``_estimated_bytes``) plus the LRU ordering are
    read-modify-write state that would otherwise drift under that concurrency,
    softening the 64 MiB bound. The lock makes both bounds hard guarantees.
    """

    MAX_ENTRIES = 1024
    MAX_ESTIMATED_BYTES = 64 * 1024 * 1024
    _data: OrderedDict[str, _CacheEntry] = OrderedDict()
    _estimated_bytes = 0
    _lock = threading.Lock()

    @staticmethod
    def _signature(path: Path) -> tuple[int, int, int] | None:
        try:
            st = path.stat()
            return st.st_ino, st.st_mtime_ns, st.st_size
        except OSError:
            return None

    @classmethod
    def _estimate(cls, value: Any) -> int:
        """Estimate unknown data iteratively, avoiding recursive call overhead."""
        total = 0
        stack = [value]
        while stack:
            current = stack.pop()
            if current is None:
                continue
            if isinstance(current, str):
                total += len(current.encode("utf-8", errors="replace")) + 49
            elif isinstance(current, (int, float, bool)):
                total += 28
            elif isinstance(current, dict):
                total += 64
                stack.extend(current.keys())
                stack.extend(current.values())
            elif isinstance(current, (list, tuple)):
                total += 64
                stack.extend(current)
            else:
                total += 64
        return total

    @classmethod
    def _estimate_scan_data(cls, data: dict[str, Any]) -> int:
        """Use the Rust engine's already-computed estimate when available."""
        hint = data.get("_cache_estimated_bytes")
        if type(hint) is int and hint > 0:
            return hint
        return cls._estimate(data)

    @classmethod
    def _discard_locked(cls, key: str) -> None:
        """Remove ``key`` and adjust the byte counter. Caller must hold the lock."""
        entry = cls._data.pop(key, None)
        if entry is not None:
            cls._estimated_bytes -= entry.estimated_bytes

    @classmethod
    def get(cls, path: Path) -> dict[str, Any] | None:
        key = os.fspath(path)
        current = cls._signature(path)
        with cls._lock:
            entry = cls._data.get(key)
            if entry is None:
                return None
            if entry.signature is not None and current != entry.signature:
                cls._discard_locked(key)
                return None
            cls._data.move_to_end(key)
            return entry.data

    @classmethod
    def set(cls, path: Path, data: dict[str, Any]) -> None:
        key = os.fspath(path)
        estimated = cls._estimate_scan_data(data)
        if estimated > cls.MAX_ESTIMATED_BYTES:
            # Oversized: never cache it, but still drop any stale entry for this key.
            with cls._lock:
                cls._discard_locked(key)
            return
        signature = cls._signature(path)
        with cls._lock:
            cls._discard_locked(key)
            cls._data[key] = _CacheEntry(data, estimated, signature)
            cls._estimated_bytes += estimated
            while (
                len(cls._data) > cls.MAX_ENTRIES or cls._estimated_bytes > cls.MAX_ESTIMATED_BYTES
            ):
                _, evicted = cls._data.popitem(last=False)
                cls._estimated_bytes -= evicted.estimated_bytes

    @classmethod
    def discard(cls, path: Path) -> None:
        with cls._lock:
            cls._discard_locked(os.fspath(path))

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._data.clear()
            cls._estimated_bytes = 0
