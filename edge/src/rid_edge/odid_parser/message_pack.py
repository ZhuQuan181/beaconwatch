"""Open Drone ID Message Pack (MessageType 0xF).

Wire layout::

    byte 0  message type (high nibble 0xF) + protocol version (low nibble)
    byte 1  SingleMessageSize, always 25
    byte 2  MsgPackSize, number of 25-byte messages (1..9)
    byte 3  first complete 25-byte message
    ...

The old implementation treated byte 1 as a byte length and prepended a
second type byte to every message.  Both were wrong; official captures
therefore could not be decoded.
"""
from __future__ import annotations

import dataclasses
import enum

from rid_edge.odid_parser import (
    basic_id,
    location,
    operator_id,
    self_id,
    system,
)
from rid_edge.odid_parser.constants import (
    MAX_MESSAGES_PER_PACK,
    MESSAGE_PACK_HEADER_SIZE,
    MESSAGE_SIZE,
    PROTOCOL_VERSION,
    MsgType,
)

_SUBMESSAGE_SIZES: dict[int, int] = {
    int(MsgType.BASIC_ID): MESSAGE_SIZE,
    int(MsgType.LOCATION): MESSAGE_SIZE,
    int(MsgType.AUTH): MESSAGE_SIZE,
    int(MsgType.SELF_ID): MESSAGE_SIZE,
    int(MsgType.SYSTEM): MESSAGE_SIZE,
    int(MsgType.OPERATOR_ID): MESSAGE_SIZE,
}


class SubMessageType(enum.IntEnum):
    BASIC_ID = MsgType.BASIC_ID
    LOCATION = MsgType.LOCATION
    AUTH = MsgType.AUTH
    SELF_ID = MsgType.SELF_ID
    SYSTEM = MsgType.SYSTEM
    OPERATOR_ID = MsgType.OPERATOR_ID


@dataclasses.dataclass(slots=True)
class SubMessage:
    """One complete 25-byte encoded message.

    ``payload`` includes the message-type byte.  This mirrors what is
    placed inside a Message Pack and makes official capture vectors
    directly usable in tests.
    """

    msg_type: SubMessageType
    payload: bytes

    def __post_init__(self) -> None:
        if len(self.payload) != _SUBMESSAGE_SIZES[int(self.msg_type)]:
            raise ValueError(
                f"SubMessage type {self.msg_type.name!r} must be exactly "
                f"25 bytes, got {len(self.payload)}"
            )
        if self.payload[0] >> 4 != int(self.msg_type):
            raise ValueError(
                f"payload type nibble {self.payload[0] >> 4:#x} does not "
                f"match {self.msg_type.name}"
            )


class MessagePackParser:
    """Builder for a standards-compliant Message Pack."""

    __slots__ = ("_submessages",)

    def __init__(self) -> None:
        self._submessages: list[SubMessage] = []

    def add(self, sub: SubMessage) -> None:
        if len(self._submessages) >= MAX_MESSAGES_PER_PACK:
            raise ValueError(f"a Message Pack holds at most {MAX_MESSAGES_PER_PACK} messages")
        self._submessages.append(sub)

    def build(self) -> bytearray:
        body = bytearray()
        for sub in self._submessages:
            body.extend(sub.payload)
        if not body:
            raise ValueError("a Message Pack must contain at least one message")
        return bytearray(
            [
                (MsgType.MESSAGE_PACK << 4) | PROTOCOL_VERSION,
                MESSAGE_SIZE,
                len(self._submessages),
            ]
        ) + body

    def __len__(self) -> int:
        return MESSAGE_PACK_HEADER_SIZE + MESSAGE_SIZE * len(self._submessages)


@dataclasses.dataclass(slots=True)
class ParsedSubmessages:
    """Decoded sub-messages from one ODID capture."""

    basic_id: list[basic_id.BasicIDMessage] = dataclasses.field(default_factory=list)
    locations: list[location.LocationMessage] = dataclasses.field(default_factory=list)
    self_ids: list[self_id.SelfIDMessage] = dataclasses.field(default_factory=list)
    systems: list[system.SystemMessage] = dataclasses.field(default_factory=list)
    operator_ids: list[operator_id.OperatorIDMessage] = dataclasses.field(
        default_factory=list
    )
    raw_blobs: list[tuple[int, bytes]] = dataclasses.field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.basic_id
            or self.locations
            or self.self_ids
            or self.systems
            or self.operator_ids
        )


def decode(payload: bytes) -> ParsedSubmessages | None:
    """Decode a complete Message Pack."""
    if len(payload) < MESSAGE_PACK_HEADER_SIZE:
        return None
    if payload[0] >> 4 != MsgType.MESSAGE_PACK:
        return None

    single_size = payload[1]
    count = payload[2]
    if single_size != MESSAGE_SIZE:
        return None
    if not 1 <= count <= MAX_MESSAGES_PER_PACK:
        return None
    expected = MESSAGE_PACK_HEADER_SIZE + single_size * count
    if len(payload) < expected:
        return None

    out = ParsedSubmessages()
    offset = MESSAGE_PACK_HEADER_SIZE
    for _ in range(count):
        chunk = payload[offset : offset + MESSAGE_SIZE]
        offset += MESSAGE_SIZE
        message_type = chunk[0] >> 4
        _decode_one(out, message_type, chunk)

    return out if not out.is_empty() else None


def _decode_one(out: ParsedSubmessages, message_type: int, chunk: bytes) -> None:
    if message_type == MsgType.BASIC_ID:
        decoded = basic_id.decode(chunk)
        if decoded is not None:
            out.basic_id.append(decoded)
            return
    elif message_type == MsgType.LOCATION:
        decoded = location.decode(chunk)
        if decoded is not None:
            out.locations.append(decoded)
            return
    elif message_type == MsgType.SELF_ID:
        decoded = self_id.decode(chunk)
        if decoded is not None:
            out.self_ids.append(decoded)
            return
    elif message_type == MsgType.SYSTEM:
        decoded = system.decode(chunk)
        if decoded is not None:
            out.systems.append(decoded)
            return
    elif message_type == MsgType.OPERATOR_ID:
        decoded = operator_id.decode(chunk)
        if decoded is not None:
            out.operator_ids.append(decoded)
            return

    # Auth is intentionally preserved as a raw blob; Self-ID and the
    # identity/location messages are the fields the dashboard needs.
    out.raw_blobs.append((message_type, chunk))


__all__ = [
    "MessagePackParser",
    "ParsedSubmessages",
    "SubMessage",
    "SubMessageType",
    "decode",
]
