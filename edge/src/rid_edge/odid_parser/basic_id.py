"""Basic ID message (MessageType 0x0).

Per ASTM F3411-22 §A.2.1 the layout is exactly 25 bytes:

    offset  size  field
    ------  ----  -----
       0     1    MessageType (high nibble) | ProtocolVersion (low nibble)
       1    18    UAS ID (ASCII, zero-padded on the right)
      19     1    Reserved (0)
      20     1    IDType (low 3 bits) | UAType (low 4 bits, top bit reserved)
      21     1    Reserved
      22     1    Reserved
      23     1    Reserved
      24     1    Reserved

Many real-world implementations actually pack this slightly differently;
the spec also defines an "alternative" layout where IDType/UAType live in
byte 1.  We follow the layout above, which is the one used by the
ODID reference C implementation.
"""
from __future__ import annotations

import dataclasses
from typing import ClassVar

from rid_edge.odid_parser.constants import IDType, UAType


@dataclasses.dataclass(slots=True, frozen=True)
class BasicIDMessage:
    """Decoded Basic ID message."""

    SIZE: ClassVar[int] = 25

    id_type: IDType
    ua_type: UAType
    uas_id: str

    def is_valid(self) -> bool:
        """Reject the all-zero "no remote ID" beacon that some chips emit."""
        return (
            self.id_type != IDType.NONE
            and self.ua_type != UAType.NONE
            and bool(self.uas_id.strip("\x00 "))
        )

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return (
            f"BasicIDMessage(uas_id={self.uas_id!r}, "
            f"ua_type={self.ua_type.label}, id_type={self.id_type.name})"
        )


def decode(payload: bytes) -> BasicIDMessage | None:
    """Decode a Basic ID payload.  Returns None on malformed input.

    `payload` MUST be exactly `SIZE` bytes (i.e. the caller has already
    stripped the per-frame message-type envelope).  In practice the
    MessagePack parser strips that; the raw-frame parser just passes
    each 25-byte slice.
    """
    if len(payload) < BasicIDMessage.SIZE:
        return None

    # bytes 1..19 = 18 ASCII chars (zero-padded on the right)
    uas_id_bytes = payload[1:19]
    try:
        uas_id = uas_id_bytes.decode("ascii").rstrip("\x00 ").strip()
    except UnicodeDecodeError:
        return None

    if not uas_id:
        return None

    # byte 20: low 3 bits = IDType, low 4 bits of byte 21 = UAType
    id_type_raw = payload[20] & 0x07
    ua_type_raw = payload[21] & 0x0F

    try:
        id_type = IDType(id_type_raw)
        ua_type = UAType(ua_type_raw)
    except ValueError:
        return None

    msg = BasicIDMessage(id_type=id_type, ua_type=ua_type, uas_id=uas_id)
    return msg if msg.is_valid() else None
