"""Tests for ULID, dedup, and backoff utilities."""
from __future__ import annotations

import asyncio
import time

import pytest
from rid_edge.common import Deduper, exponential_backoff, new_ulid
from rid_edge.common.ulid import timestamp_from_ulid


def test_ulid_length():
    assert len(new_ulid()) == 26


def test_ulid_uniqueness():
    ids = {new_ulid() for _ in range(10_000)}
    assert len(ids) == 10_000


def test_ulid_is_sortable_by_time():
    a = new_ulid()
    time.sleep(0.005)  # 5ms gap so timestamp differs
    b = new_ulid()
    assert a < b  # lexicographic == chronological


def test_ulid_timestamp_round_trip():
    before = int(time.time() * 1000)
    u = new_ulid()
    after = int(time.time() * 1000)
    ts = timestamp_from_ulid(u)
    assert before <= ts <= after


def test_dedup_blocks_within_window():
    d: Deduper[str, int] = Deduper(window_s=1.0)
    assert d.seen("a", 1) is False
    assert d.seen("a", 2) is True  # dedup
    assert d.seen("a", 3) is True


def test_dedup_allows_after_window():
    d: Deduper[str, int] = Deduper(window_s=0.05)
    assert d.seen("a", 1) is False
    time.sleep(0.08)
    assert d.seen("a", 2) is False


def test_dedup_tracks_independent_keys():
    d: Deduper[str, int] = Deduper(window_s=1.0)
    assert d.seen("a", 1) is False
    assert d.seen("b", 2) is False
    assert d.seen("a", 3) is True
    assert d.seen("b", 4) is True


def test_dedup_evicts_when_full():
    d: Deduper[int, int] = Deduper(window_s=0.01, max_keys=8, sweep_every=999)
    for i in range(8):
        assert d.seen(i, i) is False
    assert len(d) == 8
    # Wait for window to expire, then add new keys to trigger eviction
    time.sleep(0.05)
    for i in range(8, 12):
        d.seen(i, i)
    # Old keys (0..7) had stale last_seen; new keys (8..11) added.
    # Eviction should have removed at least some of the old ones.
    assert len(d) <= 8


def test_dedup_rejects_bad_window():
    with pytest.raises(ValueError):
        Deduper(window_s=0)


def test_dedup_stats():
    d: Deduper[str, int] = Deduper(window_s=2.5, max_keys=128)
    s = d.stats()
    assert s["window_s"] == 2
    assert s["max_keys"] == 128
    assert s["tracked_keys"] == 0


def test_backoff_grows_then_caps():
    delays = [exponential_backoff.compute_delay(i, base_s=1.0, cap_s=10.0, jitter=0.0) for i in range(8)]
    assert delays[0] == 1.0
    assert delays[1] == 2.0
    assert delays[2] == 4.0
    assert delays[3] == 8.0
    assert delays[4] == 10.0  # capped
    assert delays[7] == 10.0


def test_backoff_jitter_stays_in_band():
    for _ in range(50):
        d = exponential_backoff.compute_delay(2, base_s=1.0, cap_s=100.0, jitter=0.1)
        assert 3.6 <= d <= 4.4  # 4.0 ± 10%


def test_sleep_with_backoff_returns_delay():
    async def runner():
        return await exponential_backoff.sleep_with_backoff(2, base_s=0.05, cap_s=1.0)
    delay = asyncio.run(runner())
    assert 0.05 <= delay <= 1.0
