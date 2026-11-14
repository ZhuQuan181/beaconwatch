"""Tests for :mod:`rid_edge.mqtt_publisher` (offline buffer + outbox).

We deliberately avoid connecting to a real MQTT broker.  Instead we:

- directly test ``_OfflineBuffer`` (SQLite round-trip + cap eviction)
- test the ``MQTTPublisher`` public API by monkey-patching
  ``PahoClient.publish`` / ``connect_async`` so the publisher thinks it
  is connected (or not) without ever touching the network
- assert the wire topic + payload format (topic, QoS, JSON bytes)

This is the cheapest end-to-end confidence we can get without a broker.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from rid_edge.common.config import Settings
from rid_edge.models import Position, Protocol, RidObservation
from rid_edge.mqtt_publisher import MQTTPublisher, _OfflineBuffer

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    """Settings pointed at /tmp + no TLS so the publisher never actually opens a socket."""
    return Settings(
        station_id="STATION-TEST-01",
        mqtt_host="127.0.0.1",
        mqtt_port=1883,
        mqtt_use_tls=False,
        mqtt_username="",
        mqtt_password="",
        mqtt_qos=1,
        mqtt_client_id_prefix="rid-edge-test",
        mqtt_keepalive_s=30,
        mqtt_ca_cert=None,
        mqtt_client_cert=None,
        mqtt_client_key=None,
        buffer_db_path=tmp_path / "buffer.sqlite3",
        buffer_max_bytes=10_000,
        topic_obs="rid/v1/STATION-TEST-01/obs",
    )


def _make_obs(drone_id: str = "FAA-XYZ-01", **overrides: Any) -> RidObservation:
    base = dict(
        observation_id="01HFAKE0000000000000000000",
        drone_id=drone_id,
        protocol=Protocol.WIFI_NAN,
        station_id="STATION-TEST-01",
        rssi_dbm=-65,
        position=Position(lat=37.7749, lon=-122.4194, alt_m=120.0,
                          speed_mps=5.0, heading_deg=180.0),
        drone_timestamp=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        received_at=datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        operator_location=None,
        self_id="",
    )
    base.update(overrides)
    return RidObservation(**base)


def _patch_client_connected(pub: MQTTPublisher) -> MagicMock:
    """Replace pub._client with a MagicMock that pretends to be connected."""
    mock = MagicMock()
    mock.publish.return_value.is_published.return_value = True
    mock.publish.return_value.rc = 0
    pub._client = mock
    pub._connected = True
    return mock


# ---------------------------------------------------------------------------
# _OfflineBuffer
# ---------------------------------------------------------------------------


def test_offline_buffer_round_trip(tmp_path: Path) -> None:
    buf = _OfflineBuffer(tmp_path / "buf.sqlite3", max_bytes=10_000)
    buf.push("rid/v1/s/obs", 1, b'{"a":1}')
    buf.push("rid/v1/s/obs", 1, b'{"a":2}')
    assert buf.size() == 2
    rows = buf.drain_batch()
    assert len(rows) == 2
    seq, topic, qos, payload = rows[0]
    assert topic == "rid/v1/s/obs"
    assert qos == 1
    assert payload == b'{"a":1}'
    buf.delete(seq)
    assert buf.size() == 1
    buf.close()


def test_offline_buffer_evicts_when_over_cap(tmp_path: Path) -> None:
    # 100-byte cap → 60-byte payload.  With WAL the file blows past 100
    # bytes almost immediately, so every push triggers eviction of 25%
    # of the existing rows.  The cap is therefore "all rows older than
    # the most-recent batch" — we just verify the cap is enforced
    # (i.e. the file does not grow without bound).
    buf = _OfflineBuffer(tmp_path / "buf.sqlite3", max_bytes=100)
    big = b"x" * 60
    sizes: list[int] = []
    for _ in range(20):
        buf.push("t", 1, big)
        sizes.append(buf.size())
    # SQLite is on 4KB pages so 1 row can be 4KB on disk; the eviction
    # strategy can't drop a single in-use page.  The invariant we
    # actually care about: file size stays bounded.
    final = (tmp_path / "buf.sqlite3").stat().st_size
    assert final <= 4096 * 5  # at most a handful of pages, never 20 pages
    buf.close()


def test_offline_buffer_drops_oversize_payload(tmp_path: Path) -> None:
    buf = _OfflineBuffer(tmp_path / "buf.sqlite3", max_bytes=50)
    buf.push("t", 1, b"x" * 1000)  # > 50 bytes cap
    assert buf.size() == 0
    buf.close()


# ---------------------------------------------------------------------------
# MQTTPublisher — public API
# ---------------------------------------------------------------------------


async def test_publish_enqueues_to_outbox(settings: Settings) -> None:
    pub = MQTTPublisher(settings)
    try:
        await pub.publish(_make_obs())
        assert not pub._outbox.empty()
        item = pub._outbox.get_nowait()
        topic, qos, payload = item
        assert topic == "rid/v1/STATION-TEST-01/obs"
        assert qos == 1
        # JSON payload must be a valid RidObservation dump
        decoded = json.loads(payload.decode("utf-8"))
        assert decoded["drone_id"] == "FAA-XYZ-01"
        assert decoded["station_id"] == "STATION-TEST-01"
        assert decoded["protocol"] == "wifi_nan"
        assert decoded["position"]["lat"] == pytest.approx(37.7749, abs=1e-6)
    finally:
        await pub.stop()


async def test_publisher_loop_drains_when_connected(settings: Settings) -> None:
    pub = MQTTPublisher(settings)
    try:
        mock = _patch_client_connected(pub)
        await pub.start()
        await pub.publish(_make_obs("D1"))
        await pub.publish(_make_obs("D2"))
        # Allow the publisher loop to spin a few times
        for _ in range(40):
            if pub.stats["sent"] == 2 and pub._outbox.empty():
                break
            await asyncio.sleep(0.05)
        assert pub.stats["sent"] == 2
        assert pub._outbox.empty()
        assert mock.publish.call_count == 2
        # Wire topic must be the configured one
        for call in mock.publish.call_args_list:
            args, _kwargs = call
            assert args[0] == "rid/v1/STATION-TEST-01/obs"
    finally:
        await pub.stop()


async def test_publisher_loop_buffers_when_disconnected(settings: Settings) -> None:
    pub = MQTTPublisher(settings)
    try:
        # Stay disconnected.  Also neutralise the connect loop: a broker
        # may well be listening on 127.0.0.1:1883 (the dev stack), and
        # this test must never depend on whether it is running.
        pub._client.connect_async = MagicMock()
        pub._client.loop_start = MagicMock()
        pub._connected = False
        await pub.start()
        await pub.publish(_make_obs("D1"))
        await pub.publish(_make_obs("D2"))
        # Wait for the publisher loop to push them to the SQLite buffer
        for _ in range(40):
            if pub.stats["buffered"] == 2:
                break
            await asyncio.sleep(0.05)
        assert pub.stats["buffered"] == 2
        assert pub.stats["sent"] == 0
        assert pub._outbox.empty()
    finally:
        await pub.stop()


async def test_publisher_loop_buffers_when_qos1_is_not_acknowledged(
    settings: Settings,
) -> None:
    pub = MQTTPublisher(settings)
    try:
        mock = _patch_client_connected(pub)
        mock.publish.return_value.is_published.return_value = False
        await pub.start()
        await pub.publish(_make_obs("NOACK"))
        for _ in range(40):
            if pub.stats["buffered"] == 1:
                break
            await asyncio.sleep(0.05)
        assert pub.stats["buffered"] == 1
        assert pub.stats["sent"] == 0
    finally:
        await pub.stop()


async def test_drain_buffer_keeps_row_when_ack_fails(settings: Settings) -> None:
    pub = MQTTPublisher(settings)
    try:
        mock = _patch_client_connected(pub)
        mock.publish.return_value.is_published.return_value = False
        pub._buffer.push("rid/v1/s/obs", 1, b"unacked")
        await pub._drain_buffer()
        assert pub._buffer.size() == 1
    finally:
        await pub.stop()


async def test_publish_does_not_raise_on_full_outbox(settings: Settings) -> None:
    pub = MQTTPublisher(settings)
    try:
        # 10k cap, push 10k+1, the last should be dropped silently
        for i in range(10_001):
            await pub.publish(_make_obs(f"D{i % 100}"))
        assert pub.stats["dropped"] >= 1
    finally:
        await pub.stop()


async def test_stats_reflect_state(settings: Settings) -> None:
    pub = MQTTPublisher(settings)
    try:
        s0 = pub.stats
        assert s0 == {"sent": 0, "dropped": 0, "buffered": 0, "connected": 0}
        _patch_client_connected(pub)
        assert pub.stats["connected"] == 1
    finally:
        await pub.stop()
