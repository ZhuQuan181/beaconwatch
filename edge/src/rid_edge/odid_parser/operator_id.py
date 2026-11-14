"""Operator ID message (MessageType 0x5), exactly 25 bytes.

    byte 0       type/protocol
    byte 1       operator ID type
    bytes 2..21  20-byte operator ID, ASCII, NUL padded
    bytes 22..24 reserved
"""
from __future__ import annotations

import dataclasses
from typing import ClassVar

from rid_edge.odid_parser.constants import MESSAGE_SIZE, MsgType


@dataclasses.dataclass(slots=True, frozen=True)
class OperatorIDMessage:
    SIZE: ClassVar[int] = MESSAGE_SIZE

    operator_id_type: int
    operator_id: str
    protocol_version: int = 2

    def is_valid(self) -> bool:
        return bool(self.operator_id.strip("\x00 "))


def decode(payload: bytes) -> OperatorIDMessage | None:
    if len(payload) < OperatorIDMessage.SIZE:
        return None
    if payload[0] >> 4 != MsgType.OPERATOR_ID:
        return None

    operator_id = payload[2:22].decode("ascii", errors="replace")
    operator_id = operator_id.rstrip("\x00 ").strip()
    if not operator_id:
        return None

    return OperatorIDMessage(
        operator_id_type=payload[1],
        operator_id=operator_id,
        protocol_version=payload[0] & 0x0F,
    )


__all__ = ["OperatorIDMessage", "decode"]
