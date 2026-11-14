"""Open Drone ID (ASTM F3411 / ASD-STAN EN 4709-002) constants.

The wire format is defined by the Open Drone ID message marshalling
specification.  Every encoded message is exactly 25 bytes.  A Message
Pack has a three-byte header followed by ``MsgPackSize`` 25-byte
messages; it is not a length-prefixed byte stream.
"""
from __future__ import annotations

from enum import IntEnum

# Wi-Fi vendor-specific element.
WLAN_EID_VENDOR_SPECIFIC: int = 0xDD
ODID_OUI: bytes = b"\xFA\x0B\xBC"
ODID_OUI_TYPE: int = 0x0D

# NAN service discovery (Wi-Fi Alliance public action frame).
WIFI_ALLIANCE_OUI: bytes = b"\x50\x6F\x9A"
NAN_OUI_TYPE: int = 0x13
NAN_SERVICE_ID: bytes = b"\x88\x69\x19\x9D\x92\x09"
NAN_SERVICE_DESCRIPTOR_ATTRIBUTE: int = 0x03

# BLE legacy advertising service data.
BLE_AD_TYPE_SERVICE_DATA_16: int = 0x16
BLE_ODID_SERVICE_UUID_16: int = 0xFFFA

MESSAGE_SIZE: int = 25
MESSAGE_PACK_HEADER_SIZE: int = 3
MAX_MESSAGES_PER_PACK: int = 9
PROTOCOL_VERSION: int = 2


class MsgType(IntEnum):
    BASIC_ID = 0x0
    LOCATION = 0x1
    AUTH = 0x2
    SELF_ID = 0x3
    SYSTEM = 0x4
    OPERATOR_ID = 0x5
    MESSAGE_PACK = 0xF


class UAType(IntEnum):
    NONE = 0
    AEROPLANE = 1
    HELICOPTER_OR_MULTIROTOR = 2
    GYROPLANE = 3
    HYBRID_LIFT = 4
    ORNITHOPTER = 5
    GLIDER = 6
    KITE = 7
    FREE_BALLOON = 8
    CAPTIVE_BALLOON = 9
    AIRSHIP = 10
    FREE_FALL_PARACHUTE = 11
    ROCKET = 12
    TETHERED_POWERED_AIRCRAFT = 13
    GROUND_OBSTACLE = 14
    OTHER = 15

    @property
    def label(self) -> str:
        return self.name.replace("_", " ").title()


class IDType(IntEnum):
    NONE = 0
    SERIAL_NUMBER = 1
    CAA_REGISTRATION_ID = 2
    UTM_ASSIGNED_UUID = 3
    SPECIFIC_SESSION_ID = 4


class LSStatus(IntEnum):
    UNDECLARED = 0
    GROUND = 1
    AIRBORNE = 2
    EMERGENCY = 3
    REMOTE_ID_SYSTEM_FAILURE = 4


class HeightReference(IntEnum):
    OVER_TAKEOFF = 0
    OVER_GROUND = 1


class SelfIDType(IntEnum):
    TEXT = 0
    EMERGENCY = 1
    EXTENDED_STATUS = 2


class OperatorLocationType(IntEnum):
    TAKEOFF = 0
    LIVE_GNSS = 1
    FIXED = 2


class ClassificationType(IntEnum):
    UNDECLARED = 0
    EU = 1


class EUCategory(IntEnum):
    UNDECLARED = 0
    OPEN = 1
    SPECIFIC = 2
    CERTIFIED = 3


class EUClass(IntEnum):
    UNDECLARED = 0
    CLASS_0 = 1
    CLASS_1 = 2
    CLASS_2 = 3
    CLASS_3 = 4
    CLASS_4 = 5
    CLASS_5 = 6
    CLASS_6 = 7


# Accuracy tables from the Open Drone ID C implementation.  Unknown /
# reserved values decode to the spec's conservative "worst case" value.
HORIZONTAL_ACCURACY_M: tuple[float, ...] = (
    18520.0,
    18520.0,
    7808.0,
    3704.0,
    1852.0,
    926.0,
    555.6,
    185.2,
    92.6,
    30.0,
    10.0,
    3.0,
    1.0,
    18520.0,
    18520.0,
    18520.0,
)

VERTICAL_ACCURACY_M: tuple[float, ...] = (
    150.0,
    150.0,
    45.0,
    25.0,
    10.0,
    3.0,
    1.0,
    150.0,
    150.0,
    150.0,
    150.0,
    150.0,
    150.0,
    150.0,
    150.0,
    150.0,
)

SPEED_ACCURACY_MPS: tuple[float, ...] = (
    10.0,
    10.0,
    3.0,
    1.0,
    0.3,
    10.0,
    10.0,
    10.0,
    10.0,
    10.0,
    10.0,
    10.0,
    10.0,
    10.0,
    10.0,
    10.0,
)

TIMESTAMP_ACCURACY_S: tuple[float, ...] = tuple(i / 10.0 for i in range(16))


def decode_geo(_: int) -> float:
    """Deprecated compatibility shim.

    The old parser exposed a stub with this name.  Location decoding now
    lives in :mod:`rid_edge.odid_parser.location`.
    """
    raise NotImplementedError("decode_geo is no longer part of the wire format")


__all__ = [
    "BLE_AD_TYPE_SERVICE_DATA_16",
    "BLE_ODID_SERVICE_UUID_16",
    "ClassificationType",
    "EUCategory",
    "EUClass",
    "HORIZONTAL_ACCURACY_M",
    "HeightReference",
    "IDType",
    "LSStatus",
    "MAX_MESSAGES_PER_PACK",
    "MESSAGE_PACK_HEADER_SIZE",
    "MESSAGE_SIZE",
    "MsgType",
    "NAN_OUI_TYPE",
    "NAN_SERVICE_DESCRIPTOR_ATTRIBUTE",
    "NAN_SERVICE_ID",
    "ODID_OUI",
    "ODID_OUI_TYPE",
    "OperatorLocationType",
    "PROTOCOL_VERSION",
    "SPEED_ACCURACY_MPS",
    "SelfIDType",
    "TIMESTAMP_ACCURACY_S",
    "UAType",
    "VERTICAL_ACCURACY_M",
    "WIFI_ALLIANCE_OUI",
    "WLAN_EID_VENDOR_SPECIFIC",
]
