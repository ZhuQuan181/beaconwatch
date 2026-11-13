"""Operator ID message (MessageType 0x5).

Per ASTM F3411-22 §A.2.6 the payload is 25 bytes:
    offset  size  field
    ------  ----  -----
       0     1    MessageType (0x5) | OperatorIdType (3 bits)
       1    20    OperatorId (ASCII, zero-padded)
      21     4    Reserved
"""
from __future__ import annotations

import dataclasses
from typing import ClassVar


@dataclasses.dataclass(slots=True, frozen=True)
class OperatorIDMessage:
    SIZE: ClassVar[int] = 25

    operator_id: str

    def is_valid(self) -> bool:
        return bool(self.operator_id.strip("\x00 "))


def decode(payload: bytes) -> OperatorIDMessage | None:
    if len(payload) < OperatorIDMessage.SIZE:
        return None
    oid_bytes = payload[1:21]
    try:
        oid = oid_bytes.decode("ascii", errors="replace").rstrip("\x00").strip()
    except UnicodeDecodeError:
        return None
    msg = OperatorIDMessage(operator_id=oid)
    return msg if msg.is_valid() else None
