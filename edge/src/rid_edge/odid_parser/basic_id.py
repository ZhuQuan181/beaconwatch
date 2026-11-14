"""Basic ID message (MessageType 0x0), exactly 25 bytes.

ASTM F3411 / Open Drone ID layout:

    byte 0       high nibble = message type (0), low nibble = protocol version
    byte 1       high nibble = ID type, low nibble = UA type
    bytes 2..21  20-byte UAS ID, ASCII, NUL padded
    bytes 22..24 reserved
"""
from __future__ import annotations

import dataclasses
from typing import ClassVar

from rid_edge.odid_parser.constants import (
    MESSAGE_SIZE,
    IDType,
    MsgType,
    UAType,
)


@dataclasses.dataclass(slots=True, frozen=True)
class BasicIDMessage:
    SIZE: ClassVar[int] = MESSAGE_SIZE

    id_type: IDType
    ua_type: UAType
    uas_id: str
    protocol_version: int = 2

    def is_valid(self) -> bool:
        return bool(self.uas_id.strip("\x00 "))

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        ua = self.ua_type.label if isinstance(self.ua_type, UAType) else self.ua_type
        return (
            f"BasicIDMessage(uas_id={self.uas_id!r}, ua_type={ua}, "
            f"id_type={self.id_type})"
        )


def decode(payload: bytes) -> BasicIDMessage | None:
    """Decode a complete 25-byte Basic ID message."""
    if len(payload) < BasicIDMessage.SIZE:
        return None
    message_type = payload[0] >> 4
    if message_type != MsgType.BASIC_ID:
        return None

    id_type_raw = payload[1] >> 4
    ua_type_raw = payload[1] & 0x0F
    try:
        id_type = IDType(id_type_raw)
        ua_type = UAType(ua_type_raw)
    except ValueError:
        # Reserved values from newer revisions should not crash ingest.
        return None

    uas_id_raw = payload[2:22]
    try:
        uas_id = uas_id_raw.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        uas_id = uas_id_raw.decode("ascii", errors="replace")
    uas_id = uas_id.rstrip("\x00 ").strip()
    if not uas_id:
        return None

    return BasicIDMessage(
        id_type=id_type,
        ua_type=ua_type,
        uas_id=uas_id,
        protocol_version=payload[0] & 0x0F,
    )


__all__ = ["BasicIDMessage", "decode"]
