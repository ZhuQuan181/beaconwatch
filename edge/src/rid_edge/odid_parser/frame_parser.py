"""Raw-frame parsers: 802.11 vendor IE → ODID sub-messages and
BLE service-data 0x16/UUID 0xFFFA → ODID sub-messages.

The high-level entry points return the same `ParsedSubmessages`
structure as the MessagePack decoder so the pipeline downstream is
identical for both transports.
"""
from __future__ import annotations

import struct

from scapy.layers.dot11 import Dot11Elt

from rid_edge.odid_parser import basic_id, location, message_pack, self_id
from rid_edge.odid_parser.constants import (
    BLE_AD_TYPE_SERVICE_DATA_16,
    BLE_ODID_SERVICE_UUID_16,
    ODID_OUI,
    WLAN_EID_VENDOR_SPECIFIC,
    MsgType,
)


# -----------------------------------------------------------------------
# Wi-Fi
# -----------------------------------------------------------------------
def _is_odid_elt(elt: Dot11Elt) -> bool:
    """Return True if the 802.11 element is an ODID vendor-specific IE."""
    if elt.ID != WLAN_EID_VENDOR_SPECIFIC:
        return False
    # Vendor IE layout: 1B ID, 1B len, 3B OUI, 1B OUI-type, then payload
    if len(elt.info) < 4:
        return False
    return elt.info[:3] == ODID_OUI


def _elt_odid_payload(elt: Dot11Elt) -> bytes:
    """Return the ODID message-pack payload inside a vendor IE."""
    # elt.info already excludes the element ID + length. Strip the OUI.
    return elt.info[3:]


def parse_wifi_frame(pkt) -> message_pack.ParsedSubmessages | None:
    """Parse a scapy Dot11 packet and return any ODID sub-messages it carries.

    Returns None if the frame carries no ODID payload.
    """
    if not pkt.haslayer(Dot11Elt):
        return None

    merged = message_pack.ParsedSubmessages()
    found_any = False

    for elt in pkt[Dot11Elt]:  # scapy iterates all IEs
        if not _is_odid_elt(elt):
            continue
        odid_bytes = _elt_odid_payload(elt)
        parsed = _parse_odid_bytes(odid_bytes)
        if parsed is not None and not parsed.is_empty():
            merged.basic_id.extend(parsed.basic_id)
            merged.locations.extend(parsed.locations)
            merged.self_ids.extend(parsed.self_ids)
            merged.raw_blobs.extend(parsed.raw_blobs)
            found_any = True

    return merged if found_any else None


# -----------------------------------------------------------------------
# BLE
# -----------------------------------------------------------------------
def parse_ble_advertisement(ad: bytes) -> message_pack.ParsedSubmessages | None:
    """Parse a BLE advertisement payload (the part after the 6-byte header,
    starting at byte 0 of the advertising data)."""
    merged = message_pack.ParsedSubmessages()
    found_any = False

    offset = 0
    n = len(ad)
    while offset + 2 <= n:
        length = ad[offset]
        if length == 0:
            break
        ad_type = ad[offset + 1]
        ad_data = ad[offset + 2 : offset + 1 + length]
        if (
            ad_type == BLE_AD_TYPE_SERVICE_DATA_16
            and len(ad_data) >= 2
            and struct.unpack_from("<H", ad_data, 0)[0] == BLE_ODID_SERVICE_UUID_16
        ):
            # The ODID sub-messages start after the 2-byte UUID.
            parsed = _parse_odid_bytes(ad_data[2:])
            if parsed is not None and not parsed.is_empty():
                merged.basic_id.extend(parsed.basic_id)
                merged.locations.extend(parsed.locations)
                merged.self_ids.extend(parsed.self_ids)
                merged.raw_blobs.extend(parsed.raw_blobs)
                found_any = True
        offset += 1 + length
    return merged if found_any else None


# -----------------------------------------------------------------------
# Shared dispatcher
# -----------------------------------------------------------------------
def _parse_odid_bytes(buf: bytes) -> message_pack.ParsedSubmessages | None:
    """Dispatch the top-level ODID message type and decode accordingly.

    A single ODID frame carries a top-level message-type byte at offset 0.
    For most messages the type is stored in the *high nibble* of that byte
    (the low nibble holds the protocol version), but the MessagePack
    envelope uses the full byte value 0x0F.  We therefore check the
    MessagePack case first using a full-byte comparison.
    """
    if not buf:
        return None
    # MessagePack (MessageType 0xF) is the only envelope that uses the
    # full byte value rather than the high nibble.
    if buf[0] == MsgType.MESSAGE_PACK:
        return message_pack.decode(buf)
    mt = buf[0] & 0xF0  # top nibble
    if mt == MsgType.BASIC_ID << 4:
        b = basic_id.decode(buf)
        if b is None:
            return None
        return message_pack.ParsedSubmessages(basic_id=[b])
    if mt == MsgType.LOCATION << 4:
        loc = location.decode(buf)
        if loc is None:
            return None
        return message_pack.ParsedSubmessages(locations=[loc])
    if mt == MsgType.SELF_ID << 4:
        s = self_id.decode(buf)
        if s is None:
            return None
        return message_pack.ParsedSubmessages(self_ids=[s])
    # Unknown type, but we still want to surface it for debugging
    return message_pack.ParsedSubmessages(raw_blobs=[(mt, buf)])
