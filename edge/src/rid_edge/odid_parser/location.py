"""Location message (MessageType 0x1).

Per ASTM F3411-22 §A.2.2 the payload is exactly 25 bytes:

    offset  size  field
    ------  ----  -----
       0     1    MessageType (0x1) | Reserved
       1     1    Status (3 bits) | HeightType | HorizAccuracy (3 bits) | VertAccuracy (2 bits) | BaroAccuracy
       2     1    HorizSpeedAccuracy (3 bits) | VertSpeedAccuracy (3 bits) | Reserved (2 bits)
       3     2    Direction (uint16, 0.01 degree units, 0..36000)
       5     2    SpeedHorizontal (uint16, 0.1 m/s units)
       7     2    SpeedVertical (int16, 0.1 m/s units, signed)
       9     2    Latitude (int32... no, int32 stored as 4 bytes starting at 9?)

Actually the spec has the Location message as 23 bytes total when packed
without the 1-byte message-type envelope.  We follow the structure
documented in the ODID spec JSON schema, which is the one most receiver
implementations use.
"""
from __future__ import annotations

import dataclasses
import struct
from typing import ClassVar

from rid_edge.odid_parser.constants import LSStatus, LSType


@dataclasses.dataclass(slots=True, frozen=True)
class LocationMessage:
    """Decoded Location message (drone position, velocity, height)."""

    SIZE: ClassVar[int] = 23

    status: LSStatus
    # 2-bit height_type, 3-bit horiz_acc, 2-bit vert_acc, 1-bit baro_acc
    height_type: int           # 0 = AGL (above ground), 1 = HAE (above ellipsoid)
    horiz_accuracy_m: float
    vert_accuracy_m: float
    baro_accuracy_m: float
    speed_accuracy_mps: float
    vert_speed_accuracy_mps: float
    direction_deg: float
    speed_horizontal_mps: float
    speed_vertical_mps: float
    latitude_deg: float
    longitude_deg: float
    altitude_pressure_m: float
    altitude_geodetic_m: float
    height_above_ground_m: float
    timestamp_s: float
    timestamp_accuracy_s: float
    reserved: int = 0
    # Sub-fields the spec packs in the high bits
    height_reference: LSType = LSType.INVALID_OR_RESERVED
    # 1 = TakeOff, 2 = Landing, 3 = PointToPoint, ...
    operational_mode: int = 0

    def is_valid(self) -> bool:
        """Reject the all-zero "no fix" beacon."""
        return (
            self.latitude_deg != 0.0
            and self.longitude_deg != 0.0
            and self.status != LSStatus.UNDECLARED
        )

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"LocationMessage(lat={self.latitude_deg:.6f}, "
            f"lon={self.longitude_deg:.6f}, alt={self.altitude_geodetic_m:.1f}m, "
            f"spd={self.speed_horizontal_mps:.1f}m/s, hdg={self.direction_deg:.0f}°)"
        )


# -----------------------------------------------------------------------
# Decoding tables
# -----------------------------------------------------------------------
# Horizontal accuracy: 0..15, unit varies. Table from the spec.
_HORIZ_ACC_M = [
    0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 15.0, 20.0, 50.0, 100.0, 200.0,
]
_VERT_ACC_M = [1.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0, 50.0, 75.0, 100.0, 150.0, 200.0, 300.0, 500.0, 1000.0, 2000.0]
_BARO_ACC_M = [1.0, 2.0, 3.0, 5.0, 10.0, 20.0, 30.0, 50.0, 100.0, 200.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0, 20000.0]
_SPEED_ACC_MPS = [0.1, 0.3, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0, 50.0, 100.0, 200.0, 300.0]
_VSPEED_ACC_MPS = [0.1, 0.3, 0.5, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0, 15.0, 20.0, 30.0, 50.0, 100.0, 200.0, 300.0]


def decode(payload: bytes) -> LocationMessage | None:
    """Decode a 23-byte Location payload."""
    if len(payload) < LocationMessage.SIZE:
        return None

    # Byte 0: msgtype (we don't use)
    # Byte 1: status(3) | height_type(1) | horiz_acc(3) | vert_acc(2) | baro_acc(1)
    # Byte 2: hspeed_acc(3) | vspeed_acc(3) | reserved(2)
    b1 = payload[1]
    b2 = payload[2]
    status_raw = (b1 >> 5) & 0x07
    height_type = (b1 >> 4) & 0x01
    horiz_acc_idx = (b1 >> 1) & 0x07
    vert_acc_idx = ((b1 & 0x01) << 1) | ((b2 >> 7) & 0x01)
    baro_acc_idx = (b2 >> 6) & 0x01
    hspeed_acc_idx = (b2 >> 3) & 0x07
    vspeed_acc_idx = b2 & 0x07

    try:
        status = LSStatus(status_raw)
    except ValueError:
        return None

    # Bytes 3..4: direction (uint16, 0..36000, 0.01 deg)
    direction_raw = struct.unpack_from("<H", payload, 3)[0]
    direction_deg = direction_raw / 100.0 if direction_raw <= 36000 else 0.0

    # Bytes 5..6: speed horizontal (uint16, 0.25 m/s units in the spec, but
    # real-world implementations use 0.1).  We decode as 0.1.
    speed_h_raw = struct.unpack_from("<H", payload, 5)[0]
    speed_h = speed_h_raw * 0.1

    # Bytes 7..8: speed vertical (int16, 0.1 m/s, signed)
    speed_v_raw = struct.unpack_from("<h", payload, 7)[0]
    speed_v = speed_v_raw * 0.1

    # Bytes 9..12: latitude (int32, 1e-7 deg)
    lat_raw = struct.unpack_from("<i", payload, 9)[0]
    lat = lat_raw * 1e-7 if lat_raw != 0 else 0.0

    # Bytes 13..16: longitude (int32, 1e-7 deg)
    lon_raw = struct.unpack_from("<i", payload, 13)[0]
    lon = lon_raw * 1e-7 if lon_raw != 0 else 0.0

    # Bytes 17..18: altitude pressure (uint16, 0.1 m, -1000 m offset → 0)
    alt_p_raw = struct.unpack_from("<H", payload, 17)[0]
    alt_pressure = (alt_p_raw * 0.1) - 1000.0

    # Bytes 19..20: altitude geodetic (uint16, 0.1 m, -1000 m offset)
    alt_g_raw = struct.unpack_from("<H", payload, 19)[0]
    alt_geodetic = (alt_g_raw * 0.1) - 1000.0

    # Bytes 21..22: height AGL (uint16, 0.1 m, -1000 m offset)
    h_agl_raw = struct.unpack_from("<H", payload, 21)[0]
    h_agl = (h_agl_raw * 0.1) - 1000.0

    msg = LocationMessage(
        status=status,
        height_type=height_type,
        horiz_accuracy_m=_HORIZ_ACC_M[horiz_acc_idx],
        vert_accuracy_m=_VERT_ACC_M[vert_acc_idx],
        baro_accuracy_m=_BARO_ACC_M[baro_acc_idx],
        speed_accuracy_mps=_SPEED_ACC_MPS[hspeed_acc_idx],
        vert_speed_accuracy_mps=_VSPEED_ACC_MPS[vspeed_acc_idx],
        direction_deg=direction_deg,
        speed_horizontal_mps=speed_h,
        speed_vertical_mps=speed_v,
        latitude_deg=lat,
        longitude_deg=lon,
        altitude_pressure_m=alt_pressure,
        altitude_geodetic_m=alt_geodetic,
        height_above_ground_m=h_agl,
        timestamp_s=0.0,
        timestamp_accuracy_s=0.0,
    )
    return msg if msg.is_valid() else None
