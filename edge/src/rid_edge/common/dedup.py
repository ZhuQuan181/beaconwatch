"""Time-windowed dedup with FIFO eviction.

A single drone may emit several ODID messages per second over
multiple bearers (Wi-Fi NaN Action frames, Wi-Fi beacon, BLE
advertisement, Operator ID, Self ID).  The dedup coalesces
everything that arrives for the same key within ``window_s``
into one logical emission so the server is not flooded.

API
---
::

    d: Deduper[K, V] = Deduper(window_s=1.0, max_keys=4096)
    is_dup = d.seen(key, value)   # True if (key, value) is a duplicate
    d.clear()
    len(d)
    d.stats()

Semantics of ``seen()``:

- First call for a key within the window: returns ``False`` (the
  caller should *process* this record).
- Subsequent call for the same key within the window: returns
  ``True`` (the caller should *skip* / drop it).
- After the window has elapsed: the next call returns ``False``
  again (treated as a fresh emission).

Implementation
--------------
- Backed by an ``OrderedDict`` for FIFO eviction.
- Single ``threading.Lock`` (the dedup lives in the asyncio loop, so
  contention is brief and uncontended).
- Lazy sweep: we only evict expired keys every ``sweep_every``
  inserts, which keeps the amortized cost O(1) per ``seen()``.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from threading import Lock
from typing import Generic, TypeVar

K = TypeVar("K")
V = TypeVar("V")


class Deduper(Generic[K, V]):
    """Time-windowed dedup of (key, value) pairs.

    Parameters
    ----------
    window_s:
        Coalesce window (seconds).  Re-occurrences of the same key
        within this window are treated as duplicates.
    max_keys:
        Hard cap on tracked keys.  Once exceeded the *oldest* entry
        is dropped (we prefer losing history to dropping live data).
    sweep_every:
        Number of inserts between full sweeps of the entry table.
        Set high to amortize the linear scan; set to 1 to always sweep.
    """

    __slots__ = (
        "_window_s",
        "_max_keys",
        "_sweep_every",
        "_entries",
        "_lock",
        "_inserts_since_sweep",
    )

    def __init__(
        self,
        window_s: float = 1.0,
        max_keys: int = 4096,
        sweep_every: int = 64,
    ) -> None:
        if window_s <= 0:
            raise ValueError("window_s must be > 0")
        if max_keys < 1:
            raise ValueError("max_keys must be >= 1")
        if sweep_every < 1:
            raise ValueError("sweep_every must be >= 1")
        self._window_s = float(window_s)
        self._max_keys = int(max_keys)
        self._sweep_every = int(sweep_every)
        self._entries: "OrderedDict[K, tuple[float, V]]" = OrderedDict()
        self._lock = Lock()
        self._inserts_since_sweep = 0

    # ---- public API --------------------------------------------------

    def seen(self, key: K, value: V) -> bool:
        """Record (key, value) and return ``True`` if it's a duplicate.

        The *latest* value for the key is always retained, so callers
        may use this dedup as a "most recent" cache.
        """
        now = time.monotonic()
        with self._lock:
            self._inserts_since_sweep += 1
            if self._inserts_since_sweep >= self._sweep_every:
                self._evict_expired_locked(now)
                self._inserts_since_sweep = 0

            entry = self._entries.get(key)
            if entry is not None:
                first_seen, _ = entry
                if now - first_seen < self._window_s:
                    # Duplicate within window: update value, mark duplicate.
                    self._entries[key] = (first_seen, value)
                    # Move to end so recently-touched keys survive eviction.
                    self._entries.move_to_end(key)
                    return True

            # New (or window-expired): insert.
            self._entries[key] = (now, value)
            self._enforce_cap_locked()
            return False

    def get(self, key: K) -> V | None:
        """Return the latest value for ``key`` without recording anything."""
        with self._lock:
            entry = self._entries.get(key)
            return None if entry is None else entry[1]

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._inserts_since_sweep = 0

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "window_s": int(self._window_s),
                "max_keys": self._max_keys,
                "tracked_keys": len(self._entries),
            }

    # ---- internals ---------------------------------------------------

    def _evict_expired_locked(self, now: float) -> None:
        cutoff = now - self._window_s
        for k in list(self._entries.keys()):
            first_seen, _ = self._entries[k]
            if first_seen < cutoff:
                self._entries.pop(k, None)
            else:
                break

    def _enforce_cap_locked(self) -> None:
        while len(self._entries) > self._max_keys:
            self._entries.popitem(last=False)


__all__ = ["Deduper"]
