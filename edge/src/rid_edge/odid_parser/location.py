"""Location message (MessageType 0x1), exactly 25 bytes.

ASTM F3411 / Open Drone ID layout:

    byte 0       type/protocol
    byte 1       SpeedMult, EWDirection, HeightType, reserved, Status
    byte 2       direction, degrees 0..179 plus 180 when EWDirection is set
    byte 3       horizontal speed, 0.25 m/s steps (or 0.75 above 63.75 m/s)
    byte 4       vertical speed, int8 * 0.5 m/s
    bytes 5..8   latitude, int32 * 1e-7
    bytes 9..12  longitude, int32 * 1e-7
    bytes 13..14 barometric altitude, uint16 * 0.5 - 1000 m
    bytes 15..16 geodetic altitude, uint16 * 0.5 - 1000 m
    bytes 17..18 height above takeoff/ground, uint16 * 0.5 - 1000 m
    byte 19      horizontal accuracy (low nibble), vertical accuracy (high)
    byte 20      speed accuracy (low nibble), baro accuracy (high)
    bytes 21..22 timestamp, tenths of a second after the hour
    byte 23      timestamp accuracy (low nibble), reserved high nibble
    byte 24      reserved
"""
from __future__ import annotations

import dataclasses
import struct
from typing import ClassVar

from rid_edge.odid_parser.constants import (
    HORIZONTAL_ACCURACY_M,
    MESSAGE_SIZE,
    SPEED_ACCURACY_MPS,
    TIMESTAMP_ACCURACY_S,
    VERTICAL_ACCURACY_M,
    HeightReference,
    LSStatus,
    MsgType,
)


@dataclasses.dataclass(slots=True, frozen=True)
class LocationMessage:
    """Decoded drone position, velocity, height, and timestamp."""

    SIZE: ClassVar[int] = MESSAGE_SIZE

    status: LSStatus | int
    speed_mult: bool
    ew_direction: bool
    height_type: HeightReference | int
    direction_deg: float
    speed_horizontal_mps: float
    speed_vertical_mps: float
    latitude_deg: float
    longitude_deg: float
    altitude_pressure_m: float
    altitude_geodetic_m: float
    height_above_ground_m: float
    horiz_accuracy_m: float
    vert_accuracy_m: float
    baro_accuracy_m: float
    speed_accuracy_mps: float
    timestamp_s: float | None
    timestamp_accuracy_s: float
    protocol_version: int = 2
    reserved: int = 0

    def is_valid(self) -> bool:
        # The spec uses (0, 0) as "no position fix".
        return (
            not (self.latitude_deg == 0.0 and self.longitude_deg == 0.0)
            and -90.0 <= self.latitude_deg <= 90.0
            and -180.0 <= self.longitude_deg <= 180.0
        )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"LocationMessage(lat={self.latitude_deg:.6f}, "
            f"lon={self.longitude_deg:.6f}, alt={self.altitude_geodetic_m:.1f}m, "
            f"spd={self.speed_horizontal_mps:.1f}m/s, "
            f"hdg={self.direction_deg:.0f}°)"
        )


def _status(value: int) -> LSStatus | int:
    try:
        return LSStatus(value)
    except ValueError:
        return value


def _height_ref(value: int) -> HeightReference | int:
    try:
        return HeightReference(value)
    except ValueError:
        return value


def decode(payload: bytes) -> LocationMessage | None:
    """Decode a complete 25-byte Location message."""
    if len(payload) < LocationMessage.SIZE:
        return None
    if payload[0] >> 4 != MsgType.LOCATION:
        return None

    flags = payload[1]
    speed_mult = bool(flags & 0x01)
    ew_direction = bool(flags & 0x02)
    height_type = (flags >> 2) & 0x01
    status_raw = (flags >> 4) & 0x0F

    direction = float(payload[2])
    if ew_direction:
        direction += 180.0
    direction %= 360.0

    speed_raw = payload[3]
    if speed_mult:
        speed_horizontal = speed_raw * 0.75 + 255 * 0.25
    else:
        speed_horizontal = speed_raw * 0.25

    speed_vertical = struct.unpack_from("<b", payload, 4)[0] * 0.5
    latitude = struct.unpack_from("<i", payload, 5)[0] * 1e-7
    longitude = struct.unpack_from("<i", payload, 9)[0] * 1e-7
    altitude_pressure = struct.unpack_from("<H", payload, 13)[0] * 0.5 - 1000.0
    altitude_geodetic = struct.unpack_from("<H", payload, 15)[0] * 0.5 - 1000.0
    height_agl = struct.unpack_from("<H", payload, 17)[0] * 0.5 - 1000.0

    horiz_acc_idx = payload[19] & 0x0F
    vert_acc_idx = (payload[19] >> 4) & 0x0F
    speed_acc_idx = payload[20] & 0x0F
    baro_acc_idx = (payload[20] >> 4) & 0x0F

    timestamp_raw = struct.unpack_from("<H", payload, 21)[0]
    timestamp_s = None if timestamp_raw == 0xFFFF else timestamp_raw / 10.0
    ts_acc_idx = payload[23] & 0x0F

    msg = LocationMessage(
        status=_status(status_raw),
        speed_mult=speed_mult,
        ew_direction=ew_direction,
        height_type=_height_ref(height_type),
        direction_deg=direction,
        speed_horizontal_mps=speed_horizontal,
        speed_vertical_mps=speed_vertical,
        latitude_deg=latitude,
        longitude_deg=longitude,
        altitude_pressure_m=altitude_pressure,
        altitude_geodetic_m=altitude_geodetic,
        height_above_ground_m=height_agl,
        horiz_accuracy_m=HORIZONTAL_ACCURACY_M[horiz_acc_idx],
        vert_accuracy_m=VERTICAL_ACCURACY_M[vert_acc_idx],
        baro_accuracy_m=VERTICAL_ACCURACY_M[baro_acc_idx],
        speed_accuracy_mps=SPEED_ACCURACY_MPS[speed_acc_idx],
        timestamp_s=timestamp_s,
        timestamp_accuracy_s=TIMESTAMP_ACCURACY_S[ts_acc_idx],
        protocol_version=payload[0] & 0x0F,
        reserved=payload[24],
    )
    return msg if msg.is_valid() else None


__all__ = ["LocationMessage", "decode"]
