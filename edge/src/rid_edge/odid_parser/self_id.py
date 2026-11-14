"""Self-ID message (MessageType 0x3), exactly 25 bytes.

    byte 0       type/protocol
    byte 1       description type (full uint8; values above 2 are vendor-specific)
    bytes 2..24  23-byte free-text description, NUL padded
"""
from __future__ import annotations

import dataclasses
from typing import ClassVar

from rid_edge.odid_parser.constants import MESSAGE_SIZE, MsgType


@dataclasses.dataclass(slots=True, frozen=True)
class SelfIDMessage:
    SIZE: ClassVar[int] = MESSAGE_SIZE

    description_type: int
    description: str
    protocol_version: int = 2

    def is_valid(self) -> bool:
        return bool(self.description.strip("\x00 "))


def decode(payload: bytes) -> SelfIDMessage | None:
    if len(payload) < SelfIDMessage.SIZE:
        return None
    if payload[0] >> 4 != MsgType.SELF_ID:
        return None

    description = payload[2:25].decode("ascii", errors="replace")
    description = description.rstrip("\x00 ").strip()
    if not description:
        return None

    return SelfIDMessage(
        description_type=payload[1],
        description=description,
        protocol_version=payload[0] & 0x0F,
    )


__all__ = ["SelfIDMessage", "decode"]
