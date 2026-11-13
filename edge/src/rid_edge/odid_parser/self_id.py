"""Self-ID message (MessageType 0x3).

Per ASTM F3411-22 §A.2.3 the payload is 23 bytes:

    offset  size  field
    ------  ----  -----
       0     1    MessageType (0x3) | DescriptionType (5 bits)
       1    22    Description (ASCII, zero-padded)
"""
from __future__ import annotations

import dataclasses
from typing import ClassVar

from rid_edge.odid_parser.constants import SelfIDType


@dataclasses.dataclass(slots=True, frozen=True)
class SelfIDMessage:
    SIZE: ClassVar[int] = 23

    description_type: SelfIDType
    description: str

    def is_valid(self) -> bool:
        return bool(self.description.strip("\x00 "))


def decode(payload: bytes) -> SelfIDMessage | None:
    if len(payload) < SelfIDMessage.SIZE:
        return None
    desc_type_raw = payload[0] & 0x1F
    try:
        desc_type = SelfIDType(desc_type_raw)
    except ValueError:
        return None
    desc_bytes = payload[1:23]
    try:
        desc = desc_bytes.decode("ascii", errors="replace").rstrip("\x00").strip()
    except UnicodeDecodeError:
        return None
    msg = SelfIDMessage(description_type=desc_type, description=desc)
    return msg if msg.is_valid() else None
