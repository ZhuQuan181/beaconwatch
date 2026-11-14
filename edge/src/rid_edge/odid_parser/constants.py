"""Open Drone ID (ASTM F3411) constants.

ODID uses a packed binary format inside Wi-Fi vendor-specific IEs and
BLE AD structures. Each message has a 1-byte MessageType followed by
a type-specific payload. The full reference is the
"Open Drone ID Message Marshalling Specification" (published 2021-04-06).

All values here are taken directly from that spec.
"""
from __future__ import annotations

from enum import IntEnum

# -----------------------------------------------------------------------
# Wi-Fi vendor IE OUI
# -----------------------------------------------------------------------
# This 3-byte OUI identifies a vendor-specific information element that
# contains ODID messages. (Also used as the "manufacturer" in some IEs.)
ODID_OUI: bytes = b"\xFA\x0B\xBC"

# The 802.11 element ID for "vendor specific" is 221 (0xDD).
WLAN_EID_VENDOR_SPECIFIC: int = 0xDD


# -----------------------------------------------------------------------
# BLE AD structure
# -----------------------------------------------------------------------
# AD type 0x16 = Service Data (16-bit UUID), used as the "envelope" for
# BLE4 legacy advertisements. The 16-bit UUID 0xFFFA was reserved by
# the Bluetooth SIG for Open Drone ID. BLE5 extended advertisements
# use AD type 0x07 (Complete List of 128-bit Service UUIDs) etc., but
# almost all real drones use 0x16 + 0xFFFA.
BLE_AD_TYPE_SERVICE_DATA_16: int = 0x16
BLE_ODID_SERVICE_UUID_16: int = 0xFFFA


# -----------------------------------------------------------------------
# Message types
# -----------------------------------------------------------------------
class MsgType(IntEnum):
    BASIC_ID = 0x0
    LOCATION = 0x1
    AUTH = 0x2  # optional, deprecated by spec, we still decode
    SELF_ID = 0x3
    SYSTEM = 0x4
    OPERATOR_ID = 0x5
    MESSAGE_PACK = 0xF


# -----------------------------------------------------------------------
# Basic ID: UA type (4 bits)
# -----------------------------------------------------------------------
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


# -----------------------------------------------------------------------
# Basic ID: ID type (3 bits)
# -----------------------------------------------------------------------
class IDType(IntEnum):
    NONE = 0
    SERIAL_NUMBER = 1          # manufacturer's serial
    CAA_REGISTRATION_ID = 2    # e.g. FAA Part 107 registration
    UTM_ASSIGNED_UUID = 3
    SPECIFIC_SESSION_ID = 4    # 20-byte session ID


# -----------------------------------------------------------------------
# Location: Status (3 bits)
# -----------------------------------------------------------------------
class LSStatus(IntEnum):
    UNDECLARED = 0
    GROUND = 1
    AIRBORNE = 2
    EMERGENCY = 3
    REMOTE_ID_SYSTEM_FAILURE = 4


# -----------------------------------------------------------------------
# Location: Type flags
# -----------------------------------------------------------------------
class LSType(IntEnum):
    INVALID_OR_RESERVED = 0
    TAKEOFF = 1
    LANDING = 2
    POINT_TO_POINT = 3
    WAVEAROUND = 4
    HOVER = 5            # 2D
    HOVER_GROUND = 6
    HOVER_AIRBORNE = 7
    OTHER = 8


# -----------------------------------------------------------------------
# Location: Speed/Vertical accuracy (4 bits, packed)
# -----------------------------------------------------------------------
# Multiplier: 0.1 m/s for horizontal, 0.1 m/s for vertical (per the
# horizontal_speed_accuracy table). The exact table is documented in
# the spec; we just expose the value as a float and let the caller
# scale.


# -----------------------------------------------------------------------
# Self-ID: Description type (8 bits)
# -----------------------------------------------------------------------
class SelfIDType(IntEnum):
    TEXT = 0
    EMERGENCY = 1
    EXTENDED_STATUS = 2
    # 3-200 reserved
    # 201-255 vendor-specific


# -----------------------------------------------------------------------
# System: Classification (3 bits)
# -----------------------------------------------------------------------
class SysClassification(IntEnum):
    UNDECLARED = 0
    EU_C0 = 1   # Open category subcategory A
    EU_C1 = 2
    EU_C2 = 3
    EU_C3 = 4
    EU_C4 = 5
    EU_C5 = 6
    EU_C6 = 7


# -----------------------------------------------------------------------
# Geometry scale: encoded as int8, scale = (value & 0x0F) - 9
# gives the exponent such that meters = value * 10^(scale - 4)
# -----------------------------------------------------------------------
def decode_geo(encoded: int) -> float:
    """Decode a 4-bit signed-4bit-exponent geometry value (Location msg).

    Encoding per ASTM F3411-22 §A.2.4.4:
        encoded = ((mantissa & 0x0F) << 4) | (exponent & 0x0F)
    Resolution = 0.1 m. Range scales by exponent.
    We return the value in meters as a float, or NaN for "invalid".

    For our purposes we keep this simplified to a reasonable approximation;
    full decoding is in `odid_parser/location.py`.
    """
    # Stub: real implementation lives in location.py
    raise NotImplementedError


# -----------------------------------------------------------------------
# Operator ID: Operator Location type
# -----------------------------------------------------------------------
class OpLocType(IntEnum):
    INVALID = 0
    TAKEOFF = 1
    DYNAMIC = 2
    FIXED = 3
