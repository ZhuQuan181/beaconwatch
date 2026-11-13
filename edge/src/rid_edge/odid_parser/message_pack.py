"""MessagePack decoder (MessageType 0xF).

A MessagePack wraps multiple ODID sub-messages inside a single 802.11
vendor IE or BLE service-data payload.  Format:

    byte 0   : MessageType = 0xF
    byte 1   : size (number of bytes following, not including this byte)
    bytes 2..N : concatenation of ODID sub-messages, each starting
                 with its own 1-byte MessageType.

Because ODID messages are NOT self-describing (every type has a fixed
size), the caller has to know each type's size to split the buffer.
We hard-code them here.

This module also exposes an *encoder* (``MessagePackParser``) that
builds a MessagePack from a list of ``SubMessage``s.  It is used by
the test suite and by tooling that synthesises Wi-Fi frames.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import ClassVar

from rid_edge.odid_parser import basic_id, location, self_id
from rid_edge.odid_parser.constants import MsgType


_SUBMESSAGE_SIZES: dict[int, int] = {
    MsgType.BASIC_ID: basic_id.BasicIDMessage.SIZE,
    MsgType.LOCATION: location.LocationMessage.SIZE,
    MsgType.SELF_ID: self_id.SelfIDMessage.SIZE,
    # AUTH, SYSTEM, OPERATOR_ID have similar fixed sizes; we don't
    # decode them in the v1 edge pipeline (server can do it if needed).
}


# ----------------------------------------------------------------------
# Encoder
# ----------------------------------------------------------------------
class SubMessageType(enum.IntEnum):
    """Numeric message-type for an ODID sub-message inside a MessagePack.

    Values match the high nibble of the first byte in each sub-message.
    """

    BASIC_ID = MsgType.BASIC_ID
    LOCATION = MsgType.LOCATION
    SELF_ID = MsgType.SELF_ID
    SYSTEM = MsgType.SYSTEM
    AUTH = MsgType.AUTH
    OPERATOR_ID = MsgType.OPERATOR_ID


@dataclasses.dataclass(slots=True)
class SubMessage:
    """One sub-message in a MessagePack (header-less body)."""

    msg_type: SubMessageType
    payload: bytes  # size must match the fixed length for msg_type

    def __post_init__(self) -> None:
        expected = _SUBMESSAGE_SIZES.get(int(self.msg_type))
        if expected is not None and len(self.payload) != expected:
            # For System/Auth/OperatorID we still know the size (25 each
            # per spec) but we don't decode; we let the caller pass
            # exact-length data.
            raise ValueError(
                f"SubMessage type {self.msg_type.name!r} must be exactly "
                f"{expected} bytes, got {len(self.payload)}"
            )


class MessagePackParser:
    """Builder for a MessagePack payload.

    Usage::

        mp = MessagePackParser()
        mp.add(SubMessage(SubMessageType.BASIC_ID, basic_id_bytes))
        mp.add(SubMessage(SubMessageType.LOCATION, location_bytes))
        payload = bytes(mp.build())

    The returned ``payload`` is exactly the bytes that should be placed
    inside the 802.11 vendor IE (after the OUI) or the BLE service-data
    value (after the 2-byte UUID).
    """

    __slots__ = ("_submessages",)

    def __init__(self) -> None:
        self._submessages: list[SubMessage] = []

    def add(self, sub: SubMessage) -> None:
        self._submessages.append(sub)

    def build(self) -> bytearray:
        body = bytearray()
        for s in self._submessages:
            body.append(int(s.msg_type))
            body.extend(s.payload)
        # MessagePack header: type 0xF, size = len(body)
        return bytearray([MsgType.MESSAGE_PACK, len(body)]) + body

    def __len__(self) -> int:
        return sum(1 + len(s.payload) for s in self._submessages) + 2


# ----------------------------------------------------------------------
# Decoder
# ----------------------------------------------------------------------
@dataclasses.dataclass(slots=True)
class ParsedSubmessages:
    """One or more sub-messages decoded from a single frame."""
    basic_id: list[basic_id.BasicIDMessage] = dataclasses.field(default_factory=list)
    locations: list[location.LocationMessage] = dataclasses.field(default_factory=list)
    self_ids: list[self_id.SelfIDMessage] = dataclasses.field(default_factory=list)
    raw_blobs: list[tuple[MsgType, bytes]] = dataclasses.field(default_factory=list)

    def is_empty(self) -> bool:
        return not (self.basic_id or self.locations or self.self_ids)


def decode(payload: bytes) -> ParsedSubmessages | None:
    """Decode a MessagePack payload (2 header bytes + sub-messages).

    The MessagePack body is a concatenation of sub-message blobs, each
    starting with a 1-byte message-type prefix.  The 1-byte size in the
    header is *legacy* (older firmware) and only used as a hint; we walk
    the body by the per-type known fixed size.
    """
    if len(payload) < 2:
        return None
    declared_size = payload[1]
    # Body starts at offset 2.  If declared_size matches what the
    # header says, use that; otherwise treat the rest of the buffer as
    # body.  This is robust against off-by-one in legacy encoders.
    body = payload[2 : 2 + declared_size]
    if len(body) < declared_size:
        body = payload[2:][truncated]

    out = ParsedSubmessages()
    offset = 0
    while offset < len(body):
        mt = body[offset]
        # The per-type known size does NOT include the 1-byte message-type
        # prefix that precedes each sub-message in the MessagePack body.
        # We always advance by ``1 + size`` so the next iteration picks
        # up the correct type byte.
        size = _SUBMESSAGE_SIZES.get(mt)
        if size is None:
            # Unknown / unsupported type — store the raw blob and stop.
            out.raw_blobs.append((MsgType(mt) if mt in [m.value for m in MsgType] else mt, body[offset:]))
            break
        chunk = body[offset + 1 : offset + 1 + size]
        if len(chunk) < size:
            break
        sub = _decode_one(mt, chunk)
        if sub is not None:
            if mt == MsgType.BASIC_ID:
                out.basic_id.append(sub)
            elif mt == MsgType.LOCATION:
                out.locations.append(sub)
            elif mt == MsgType.SELF_ID:
                out.self_ids.append(sub)
        offset += 1 + size
    return out if not out.is_empty() else None


def _decode_one(mt: int, chunk: bytes):
    if mt == MsgType.BASIC_ID:
        return basic_id.decode(chunk)
    if mt == MsgType.LOCATION:
        return location.decode(chunk)
    if mt == MsgType.SELF_ID:
        return self_id.decode(chunk)
    return None


__all__ = [
    "MessagePackParser",
    "SubMessage",
    "SubMessageType",
    "ParsedSubmessages",
    "decode",
]
