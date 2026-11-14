"""Transport extraction for Wi-Fi and BLE Open Drone ID captures.

Wi-Fi has two on-air forms:

* Beacon / probe response: vendor IE 221 with OUI FA:0B:BC, OUI type
  0x0D, one service-info counter byte, then a Message Pack.
* NAN public action frame: category 0x04, action 0x09, Wi-Fi Alliance
  OUI 50:6F:9A, then NAN attributes.  The ODID Message Pack lives in
  service descriptor attribute 0x03.

BLE uses service data AD type 0x16 with UUID 0xFFFA.  Bleak already
strips the UUID on most backends, but the receiver accepts both forms.
"""
from __future__ import annotations

import struct

from scapy.layers.dot11 import Dot11, Dot11Elt

from rid_edge.odid_parser import (
    basic_id,
    location,
    message_pack,
    operator_id,
    self_id,
    system,
)
from rid_edge.odid_parser.constants import (
    BLE_AD_TYPE_SERVICE_DATA_16,
    BLE_ODID_SERVICE_UUID_16,
    MESSAGE_SIZE,
    NAN_OUI_TYPE,
    NAN_SERVICE_DESCRIPTOR_ATTRIBUTE,
    NAN_SERVICE_ID,
    ODID_OUI,
    ODID_OUI_TYPE,
    WIFI_ALLIANCE_OUI,
    MsgType,
)


def _merge(into: message_pack.ParsedSubmessages, src: message_pack.ParsedSubmessages) -> None:
    into.basic_id.extend(src.basic_id)
    into.locations.extend(src.locations)
    into.self_ids.extend(src.self_ids)
    into.systems.extend(src.systems)
    into.operator_ids.extend(src.operator_ids)
    into.raw_blobs.extend(src.raw_blobs)


def _iter_elts(pkt) -> list[Dot11Elt]:
    """Return every stacked Dot11Elt layer, not just the first one."""
    out: list[Dot11Elt] = []
    elt = pkt.getlayer(Dot11Elt)
    seen: set[int] = set()
    while elt is not None and id(elt) not in seen:
        seen.add(id(elt))
        out.append(elt)
        elt = elt.payload.getlayer(Dot11Elt) if elt.payload is not None else None
    return out


def _beacon_vendor_payload(info: bytes) -> bytes | None:
    """Extract the Message Pack from an ODID beacon vendor IE."""
    if len(info) < 5 or info[:3] != ODID_OUI or info[3] != ODID_OUI_TYPE:
        return None
    rest = info[4:]
    if not rest:
        return None
    # Normal beacon: service-info counter then Message Pack.
    if rest[0] >> 4 == MsgType.MESSAGE_PACK:
        return rest
    if len(rest) >= 2 and rest[1] >> 4 in {
        int(MsgType.BASIC_ID),
        int(MsgType.LOCATION),
        int(MsgType.AUTH),
        int(MsgType.SELF_ID),
        int(MsgType.SYSTEM),
        int(MsgType.OPERATOR_ID),
        int(MsgType.MESSAGE_PACK),
    }:
        return rest[1:]
    return None


def _nan_action_payloads(body: bytes) -> list[bytes]:
    """Extract ODID Message Packs from a NAN public action frame body."""
    if len(body) < 6:
        return []
    if body[0] != 0x04 or body[1] != 0x09:
        return []
    if body[2:5] != WIFI_ALLIANCE_OUI or body[5] != NAN_OUI_TYPE:
        return []

    payloads: list[bytes] = []
    offset = 6
    while offset + 3 <= len(body):
        attribute_id = body[offset]
        attribute_len = struct.unpack_from("<H", body, offset + 1)[0]
        value_start = offset + 3
        value_end = value_start + attribute_len
        if value_end > len(body):
            break
        value = body[value_start:value_end]
        if (
            attribute_id == NAN_SERVICE_DESCRIPTOR_ATTRIBUTE
            and len(value) >= 11
            and value[:6] == NAN_SERVICE_ID
        ):
            service_info_len = value[9]
            service_info = value[10 : 10 + service_info_len]
            if len(service_info) >= 2 and service_info[1] >> 4 == MsgType.MESSAGE_PACK:
                payloads.append(service_info[1:])
        offset = value_end
    return payloads


def extract_wifi_odid_payloads(pkt) -> list[bytes]:
    """Return all ODID payloads carried by a Scapy 802.11 packet."""
    payloads: list[bytes] = []
    for elt in _iter_elts(pkt):
        info = elt.info
        if isinstance(info, str):
            info = info.encode("latin-1", errors="ignore")
        payload = _beacon_vendor_payload(bytes(info))
        if payload:
            payloads.append(payload)

    if pkt.haslayer(Dot11):
        dot11 = pkt[Dot11]
        if dot11.payload is not None:
            raw_body = bytes(dot11.payload)
            payloads.extend(_nan_action_payloads(raw_body))
    return payloads


def parse_wifi_frame(pkt) -> message_pack.ParsedSubmessages | None:
    """Parse every ODID Message Pack in a Scapy 802.11 packet."""
    merged = message_pack.ParsedSubmessages()
    found = False
    for payload in extract_wifi_odid_payloads(pkt):
        parsed = _parse_odid_bytes(payload)
        if parsed is not None and not parsed.is_empty():
            _merge(merged, parsed)
            found = True
    return merged if found else None


def extract_ble_service_payload(service_data: bytes) -> bytes | None:
    """Normalize bleak service-data bytes to an ODID Message Pack."""
    if not service_data:
        return None
    data = service_data
    # Some backends expose the 16-bit UUID; bleak usually does not.
    if len(data) >= 2 and data[:2] == b"\xfa\xff":
        data = data[2:]
    if not data:
        return None
    if data[0] >> 4 == MsgType.MESSAGE_PACK:
        return data
    if len(data) >= MESSAGE_SIZE and data[0] >> 4 in {
        int(MsgType.BASIC_ID),
        int(MsgType.LOCATION),
        int(MsgType.AUTH),
        int(MsgType.SELF_ID),
        int(MsgType.SYSTEM),
        int(MsgType.OPERATOR_ID),
    }:
        return data[:MESSAGE_SIZE]
    return None


def parse_ble_advertisement(ad: bytes) -> message_pack.ParsedSubmessages | None:
    """Parse BLE advertising data (AD structures)."""
    merged = message_pack.ParsedSubmessages()
    found = False
    offset = 0
    while offset + 2 <= len(ad):
        length = ad[offset]
        if length == 0:
            break
        end = offset + 1 + length
        if end > len(ad):
            break
        ad_type = ad[offset + 1]
        ad_data = ad[offset + 2 : end]
        if (
            ad_type == BLE_AD_TYPE_SERVICE_DATA_16
            and len(ad_data) >= 2
            and struct.unpack_from("<H", ad_data, 0)[0] == BLE_ODID_SERVICE_UUID_16
        ):
            payload = extract_ble_service_payload(ad_data[2:])
            if payload is not None:
                parsed = _parse_odid_bytes(payload)
                if parsed is not None and not parsed.is_empty():
                    _merge(merged, parsed)
                    found = True
        offset = end
    return merged if found else None


def _parse_odid_bytes(buf: bytes) -> message_pack.ParsedSubmessages | None:
    """Decode either a Message Pack or one complete 25-byte message."""
    if not buf:
        return None
    if buf[0] >> 4 == MsgType.MESSAGE_PACK:
        return message_pack.decode(buf)
    if len(buf) < MESSAGE_SIZE:
        return None

    chunk = buf[:MESSAGE_SIZE]
    message_type = chunk[0] >> 4
    out = message_pack.ParsedSubmessages()
    if message_type == MsgType.BASIC_ID:
        decoded = basic_id.decode(chunk)
        if decoded is not None:
            out.basic_id.append(decoded)
    elif message_type == MsgType.LOCATION:
        decoded = location.decode(chunk)
        if decoded is not None:
            out.locations.append(decoded)
    elif message_type == MsgType.SELF_ID:
        decoded = self_id.decode(chunk)
        if decoded is not None:
            out.self_ids.append(decoded)
    elif message_type == MsgType.SYSTEM:
        decoded = system.decode(chunk)
        if decoded is not None:
            out.systems.append(decoded)
    elif message_type == MsgType.OPERATOR_ID:
        decoded = operator_id.decode(chunk)
        if decoded is not None:
            out.operator_ids.append(decoded)
    else:
        out.raw_blobs.append((message_type, chunk))
    return out if not out.is_empty() else None


__all__ = [
    "extract_ble_service_payload",
    "extract_wifi_odid_payloads",
    "parse_ble_advertisement",
    "parse_wifi_frame",
]
