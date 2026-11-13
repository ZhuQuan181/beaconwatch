"""Tests for :mod:`rid_edge.pipeline.parser`."""
from __future__ import annotations

import struct
import time
from datetime import datetime, timezone

import pytest

from rid_edge.models import Protocol
from rid_edge.odid_parser.message_pack import (
    MessagePackParser,
    SubMessage,
    SubMessageType,
    decode,
)
from rid_edge.pipeline import PipelineManager, observations_from_frame
from rid_edge.receivers.base import Bearer, RawFrame


def _make_basic_id_payload(uas_id: str) -> bytes:
    """Build a 25-byte ASTM F3411 BasicID payload (matches our decoder).

    Per :mod:`rid_edge.odid_parser.basic_id` decoder:
      byte 0:    msg_type (0x02 = BASIC_ID | proto_v2)
      bytes 1-18:  UAS ID (18 ASCII bytes, zero-padded)
      byte 19:   reserved (0)
      byte 20:   IDType (low 3 bits)
      byte 21:   UAType (low 4 bits)
      bytes 22-24: reserved (3 zero bytes)
    Total: 25 bytes.
    """
    assert len(uas_id) <= 18
    uas_id_padded = uas_id.encode("ascii")[:18].ljust(18, b"\x00")
    return (
        bytes([0x02])        # msg_type
        + uas_id_padded      # bytes 1-18
        + bytes([0x00])      # byte 19: reserved
        + bytes([3])         # byte 20: IDType=SERIAL_NUMBER
        + bytes([4])         # byte 21: UAType=MULTIROTOR
        + b"\x00" * 3        # bytes 22-24: reserved
    )


def _make_location_payload(lat: float, lon: float, alt_m: float) -> bytes:
    """Build a 23-byte ASTM F3411 Location payload (matches our decoder).

    Per :mod:`rid_edge.odid_parser.location` decoder:
      byte 0: msg_type (0x01 = LOCATION)
      byte 1: status(3) | height_type(1) | horiz_acc(3) | vert_acc_lo(1)
      byte 2: vert_acc_hi(1) | baro_acc(1) | hspeed_acc(3) | vspeed_acc(3)
      bytes 3-4:   direction (uint16, 0.01° units, 0..36000)
      bytes 5-6:   speed_h (uint16, 0.1 m/s)
      bytes 7-8:   speed_v (int16, 0.1 m/s, signed)
      bytes 9-12:  lat (int32, 1e-7 deg, signed)
      bytes 13-16: lon (int32, 1e-7 deg, signed)
      bytes 17-18: alt_pressure (uint16, 0.1 m, +1000 m offset)
      bytes 19-20: alt_geodetic (uint16, 0.1 m, +1000 m offset)
      bytes 21-22: h_agl (uint16, 0.1 m, +1000 m offset)
    Total: 23 bytes.
    """
    assert -90.0 <= lat <= 90.0
    assert -180.0 <= lon <= 180.0
    # status=2 (Airborne), height_type=1 (Takeoff=0 baro/HAE=1)
    # horiz_acc_idx=6 → 3.0m, vert_acc_idx=7 → 10.0m (4 bits)
    # baro_acc_idx=3 → 5m, hspeed_acc_idx=4 → 1.5m/s, vspeed_acc_idx=4 → 1.5m/s
    b1 = (2 << 5) | (0 << 4) | (6 << 1) | (7 & 0x01)                # 0x4D
    b2 = (((7 >> 1) & 0x01) << 7) | (3 << 6) | (4 << 3) | 4          # 0xE4

    lat_e7 = int(round(lat * 1e7))
    lon_e7 = int(round(lon * 1e7))
    def raw_alt(m: float) -> int:
        return int(round((m + 1000.0) * 10))
    alt_p = raw_alt(alt_m)
    alt_g = raw_alt(alt_m)
    h_agl = raw_alt(max(0.0, alt_m - 5.0))

    out = bytearray(23)
    out[0] = 0x1                  # msg_type
    out[1] = b1
    out[2] = b2
    struct.pack_into("<H", out, 3, 12345)        # direction (123.45°)
    struct.pack_into("<H", out, 5, 25)           # speed_h (2.5 m/s)
    struct.pack_into("<h", out, 7, -3)           # speed_v (-0.3 m/s, signed)
    struct.pack_into("<i", out, 9, lat_e7)       # lat
    struct.pack_into("<i", out, 13, lon_e7)      # lon
    struct.pack_into("<H", out, 17, alt_p)       # alt_pressure
    struct.pack_into("<H", out, 19, alt_g)       # alt_geodetic
    struct.pack_into("<H", out, 21, h_agl)       # h_agl
    return bytes(out)


def _make_messagepack(uas_id: str, lat: float, lon: float, alt_m: float) -> bytes:
    """Build a real ASTM F3411 MessagePack wrapping BasicID + Location."""
    bid = SubMessage(
        msg_type=SubMessageType.BASIC_ID,
        payload=_make_basic_id_payload(uas_id),
    )
    loc = SubMessage(
        msg_type=SubMessageType.LOCATION,
        payload=_make_location_payload(lat, lon, alt_m),
    )
    parser = MessagePackParser()
    parser.add(bid)
    parser.add(loc)
    return bytes(parser.build())


def test_observations_from_frame_yields_basic_id_and_location() -> None:
    mp = _make_messagepack("FAA-DRONETEST-01", 37.7749, -122.4194, 120.0)
    frame = RawFrame(
        bearer=Bearer.WIFI_NAN,
        source_mac="aa:bb:cc:dd:ee:ff",
        rssi_dbm=-55,
        ts_monotonic=time.monotonic(),
        payload=mp,
        channel=6,
    )
    # Use the high-level manager so the Pydantic station_id constraint
    # is satisfied; the underlying parser returns station_id="".
    mgr = PipelineManager(station_id="STATION-TEST-01")
    obs_list = mgr.process(frame)
    assert len(obs_list) == 1
    obs = obs_list[0]
    assert obs.drone_id == "FAA-DRONETEST-01"
    assert obs.protocol == Protocol.WIFI_NAN  # bearer is Wi-Fi NAN
    assert obs.rssi_dbm == -55
    assert obs.position.lat == pytest.approx(37.7749, abs=1e-6)
    assert obs.position.lon == pytest.approx(-122.4194, abs=1e-6)
    assert obs.position.alt_m == pytest.approx(120.0, abs=1e-3)
    assert obs.station_id == "STATION-TEST-01"
    assert isinstance(obs.received_at, datetime)


def test_observations_from_empty_payload_yields_nothing() -> None:
    frame = RawFrame(
        bearer=Bearer.BLE,
        source_mac="11:22:33:44:55:66",
        rssi_dbm=-70,
        ts_monotonic=time.monotonic(),
        payload=b"",
        channel=37,
    )
    obs_list = list(observations_from_frame(frame))
    assert obs_list == []


def test_observations_from_basic_id_only_yields_partial() -> None:
    """BasicID-only frame → one observation, position=zero placeholder."""
    bid = SubMessage(
        msg_type=SubMessageType.BASIC_ID,
        payload=_make_basic_id_payload("PARTIAL-01"),
    )
    parser = MessagePackParser()
    parser.add(bid)
    payload = bytes(parser.build())
    frame = RawFrame(
        bearer=Bearer.WIFI_NAN,
        source_mac="de:ad:be:ef:00:01",
        rssi_dbm=-65,
        ts_monotonic=time.monotonic(),
        payload=payload,
        channel=11,
    )
    mgr = PipelineManager(station_id="STATION-PARTIAL")
    obs_list = mgr.process(frame)
    assert len(obs_list) == 1
    assert obs_list[0].drone_id == "PARTIAL-01"
    assert obs_list[0].station_id == "STATION-PARTIAL"


def test_observation_carries_received_at_utc() -> None:
    mp = _make_messagepack("FAA-XYZ-01", 0.0, 0.0, 0.0)
    frame = RawFrame(
        bearer=Bearer.WIFI_NAN,
        source_mac="00:11:22:33:44:55",
        rssi_dbm=-80,
        ts_monotonic=time.monotonic(),
        payload=mp,
        channel=1,
    )
    mgr = PipelineManager(station_id="STATION-UTC")
    obs_list = mgr.process(frame)
    assert len(obs_list) == 1
    obs = obs_list[0]
    assert isinstance(obs.received_at, datetime)
    assert obs.received_at.tzinfo is not None
    assert obs.received_at.tzinfo.utcoffset(obs.received_at) == timezone.utc.utcoffset(obs.received_at)
