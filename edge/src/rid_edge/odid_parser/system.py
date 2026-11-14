"""System message (MessageType 0x4), exactly 25 bytes.

    byte 0       type/protocol
    byte 1       operator location type (2 bits), classification type (3 bits)
    bytes 2..5   operator latitude, int32 * 1e-7
    bytes 6..9   operator longitude, int32 * 1e-7
    bytes 10..11 area count, uint16
    byte 12      area radius, uint8 * 10 m
    bytes 13..14 area ceiling, uint16 * 0.5 - 1000 m
    bytes 15..16 area floor, uint16 * 0.5 - 1000 m
    byte 17      EU class (low nibble), EU category (high nibble)
    bytes 18..19 operator altitude, uint16 * 0.5 - 1000 m
    bytes 20..23 timestamp, seconds since 2019-01-01T00:00:00Z
    byte 24      reserved
"""
from __future__ import annotations

import dataclasses
import struct
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from rid_edge.odid_parser.constants import (
    MESSAGE_SIZE,
    MsgType,
    OperatorLocationType,
)

ODID_EPOCH = datetime(2019, 1, 1, tzinfo=timezone.utc)


@dataclasses.dataclass(slots=True, frozen=True)
class SystemMessage:
    SIZE: ClassVar[int] = MESSAGE_SIZE

    operator_location_type: OperatorLocationType | int
    classification_type: int
    operator_lat: float
    operator_lon: float
    operator_alt_m: float
    area_count: int
    area_radius_m: float
    area_floor_m: float
    area_ceiling_m: float
    category_eu: int
    class_eu: int
    timestamp_s: int
    protocol_version: int = 2
    reserved: int = 0

    @property
    def timestamp_utc(self) -> datetime:
        return ODID_EPOCH + timedelta(seconds=self.timestamp_s)


def _operator_location_type(value: int) -> OperatorLocationType | int:
    try:
        return OperatorLocationType(value)
    except ValueError:
        return value


def decode(payload: bytes) -> SystemMessage | None:
    if len(payload) < SystemMessage.SIZE:
        return None
    if payload[0] >> 4 != MsgType.SYSTEM:
        return None

    flags = payload[1]
    operator_location_type = flags & 0x03
    classification_type = (flags >> 2) & 0x07

    operator_lat = struct.unpack_from("<i", payload, 2)[0] * 1e-7
    operator_lon = struct.unpack_from("<i", payload, 6)[0] * 1e-7
    area_count = struct.unpack_from("<H", payload, 10)[0]
    area_radius = payload[12] * 10.0
    area_ceiling = struct.unpack_from("<H", payload, 13)[0] * 0.5 - 1000.0
    area_floor = struct.unpack_from("<H", payload, 15)[0] * 0.5 - 1000.0
    category_eu = (payload[17] >> 4) & 0x0F
    class_eu = payload[17] & 0x0F
    operator_alt = struct.unpack_from("<H", payload, 18)[0] * 0.5 - 1000.0
    timestamp = struct.unpack_from("<I", payload, 20)[0]

    return SystemMessage(
        operator_location_type=_operator_location_type(operator_location_type),
        classification_type=classification_type,
        operator_lat=operator_lat,
        operator_lon=operator_lon,
        operator_alt_m=operator_alt,
        area_count=area_count,
        area_radius_m=area_radius,
        area_floor_m=area_floor,
        area_ceiling_m=area_ceiling,
        category_eu=category_eu,
        class_eu=class_eu,
        timestamp_s=timestamp,
        protocol_version=payload[0] & 0x0F,
        reserved=payload[24],
    )


__all__ = ["ODID_EPOCH", "SystemMessage", "decode"]
