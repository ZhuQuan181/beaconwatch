"""System message (MessageType 0x4).

Per ASTM F3411-22 §A.2.5 the payload is 23 bytes:
    offset  size  field
    ------  ----  -----
       0     1    MessageType (0x4) | Classification (3 bits) | Reserved
       1     4    OperatorLocationGeo (int32 lat + int32 lon, 1e-7 deg)
       5     2    OperatorAltitude (uint16, 0.1 m, -1000 m offset)
       7     2    Timestamp (uint16, 0.1 s since 00:00:00 UTC)
       9     4    Area count (uint16) | Area radius (uint16) | Area floor (uint16) | Area ceiling (uint16)
       13   10    Reserved / category
"""
from __future__ import annotations

import dataclasses
import struct
from typing import ClassVar

from rid_edge.odid_parser.constants import SysClassification


@dataclasses.dataclass(slots=True, frozen=True)
class SystemMessage:
    SIZE: ClassVar[int] = 23

    classification: SysClassification
    operator_lat: float
    operator_lon: float
    operator_alt_m: float
    timestamp_s: float
    area_count: int
    area_radius_m: float
    area_floor_m: float
    area_ceiling_m: float
    category: int
    class_ua: int


def decode(payload: bytes) -> SystemMessage | None:
    if len(payload) < SystemMessage.SIZE:
        return None
    b0 = payload[0]
    class_raw = (b0 >> 5) & 0x07
    try:
        classification = SysClassification(class_raw)
    except ValueError:
        return None

    op_lat_raw = struct.unpack_from("<i", payload, 1)[0]
    op_lon_raw = struct.unpack_from("<i", payload, 5)[0]
    op_alt_raw = struct.unpack_from("<H", payload, 9)[0]
    ts_raw = struct.unpack_from("<H", payload, 11)[0]
    area_count = struct.unpack_from("<H", payload, 13)[0]
    area_radius_raw = struct.unpack_from("<H", payload, 15)[0]
    area_floor_raw = struct.unpack_from("<H", payload, 17)[0]
    area_ceiling_raw = struct.unpack_from("<H", payload, 19)[0]
    category = payload[21]
    class_ua = payload[22]

    return SystemMessage(
        classification=classification,
        operator_lat=op_lat_raw * 1e-7,
        operator_lon=op_lon_raw * 1e-7,
        operator_alt_m=(op_alt_raw * 0.1) - 1000.0,
        timestamp_s=ts_raw * 0.1,
        area_count=area_count,
        area_radius_m=area_radius_raw * 0.5,
        area_floor_m=(area_floor_raw * 0.5) - 1000.0,
        area_ceiling_m=(area_ceiling_raw * 0.5) - 1000.0,
        category=category,
        class_ua=class_ua,
    )
