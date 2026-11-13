"""Convert :class:`RawFrame` objects into :class:`RidObservation` models.

A single Wi-Fi/BLE frame may carry multiple ODID sub-messages
(MessagePack).  This module walks the parsed sub-messages and emits
one :class:`RidObservation` per (BasicID, Location) pair.  If a frame
carries only a BasicID we still emit a partial observation (no
position); if it carries only a Location we synthesize a BasicID from
the source MAC (the drone hasn't sent its ID yet).

Identity priority
-----------------
1. BasicID's UAS ID (if present and valid)
2. Source MAC (BLE privacy / NaN)
3. `<unknown:{mac}>` placeholder
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Iterable

from ..models import (
    OperatorLocation,
    Position,
    Protocol,
    RidObservation,
)
from ..odid_parser import basic_id as basic_id_mod
from ..odid_parser import location as location_mod
from ..odid_parser import self_id as self_id_mod
from ..odid_parser.message_pack import ParsedSubmessages
from ..receivers.base import Bearer, RawFrame
from ..common.geo import is_zero_coordinate


def _drone_id_from_basic_id(b: basic_id_mod.BasicIDMessage) -> str:
    """The UAS ID is the canonical drone identifier in the system."""
    return b.uas_id.strip().rstrip("\x00") or "unknown"


def _observation_id(raw: RawFrame, uas_id: str) -> str:
    """Stable per-(frame, drone) id.  Used by the server for idempotency."""
    h = hashlib.sha256()
    h.update(raw.bearer.value.encode())
    h.update(b"|")
    h.update(raw.source_mac.encode())
    h.update(b"|")
    h.update(raw.payload[:64])
    h.update(b"|")
    h.update(uas_id.encode())
    return h.hexdigest()[:32]


def _parse_payload(raw: RawFrame) -> ParsedSubmessages | None:
    """Dispatch to the right per-bearer parser."""
    if raw.bearer == Bearer.WIFI_NAN:
        # The Wi-Fi raw_frame already extracted the ODID bytes; the
        # downstream frame_parser handles both single-message and
        # MessagePack envelopes.  We feed it as a single-byte ad_type
        # to reuse the dispatcher.
        from ..odid_parser import frame_parser
        # _parse_odid_bytes is private but exactly what we need.
        return frame_parser._parse_odid_bytes(raw.payload)  # type: ignore[attr-defined]
    if raw.bearer == Bearer.BLE:
        from ..odid_parser import frame_parser
        return frame_parser._parse_odid_bytes(raw.payload)  # type: ignore[attr-defined]
    return None


def observations_from_frame(raw: RawFrame) -> list[RidObservation]:
    """Convert one :class:`RawFrame` into zero or more :class:`RidObservation`.

    The list is empty if the frame carries no parseable BasicID +
    Location pair.
    """
    parsed = _parse_payload(raw)
    if parsed is None:
        return []

    basic_ids = parsed.basic_id or []
    locations = parsed.locations or []
    self_ids = parsed.self_ids or []

    # If no BasicID was sent, synthesize a placeholder from the MAC so
    # the location message can still produce an observation.
    if not basic_ids:
        synthetic = basic_id_mod.BasicIDMessage(
            id_type=basic_id_mod.IDType.NONE,
            ua_type=basic_id_mod.UAType.NONE,
            uas_id=f"unknown:{raw.source_mac}",
        )
        basic_ids = [synthetic]

    out: list[RidObservation] = []
    self_id_text = self_ids[0].description if self_ids else None

    for bid in basic_ids:
        for loc in locations:
            obs = _build_observation(
                raw=raw, bid=bid, loc=loc, self_id_text=self_id_text
            )
            if obs is not None:
                out.append(obs)

    # Also emit observations for BasicID-only frames (no position yet).
    if not locations:
        for bid in basic_ids:
            obs = _build_observation(
                raw=raw, bid=bid, loc=None, self_id_text=self_id_text
            )
            if obs is not None:
                out.append(obs)
    return out


def _build_observation(
    *,
    raw: RawFrame,
    bid: basic_id_mod.BasicIDMessage,
    loc: location_mod.LocationMessage | None,
    self_id_text: str | None,
) -> RidObservation | None:
    drone_id = bid.uas_id.strip().rstrip("\x00") or f"unknown:{raw.source_mac}"

    if loc is not None:
        if is_zero_coordinate(loc.latitude_deg, loc.longitude_deg):
            return None  # drone hasn't acquired a fix
        position = Position(
            lat=loc.latitude_deg,
            lon=loc.longitude_deg,
            alt_m=loc.altitude_geodetic_m,
            accuracy_h_m=loc.horiz_accuracy_m,
        )
        speed = max(0.0, loc.speed_horizontal_mps)
        heading = loc.direction_deg % 360.0
    else:
        # No Location in this frame — use the station's location with
        # a very large accuracy so the server knows it's a placeholder.
        position = Position(lat=0.0, lon=0.0, alt_m=0.0, accuracy_h_m=1e6)
        speed = 0.0
        heading = 0.0

    protocol = (
        Protocol.WIFI_NAN if raw.bearer == Bearer.WIFI_NAN else Protocol.ASTM_F3411
    )

    return RidObservation(
        observation_id=_observation_id(raw, drone_id),
        drone_id=drone_id,
        operator_id=None,  # filled in by OperatorID message in the future
        protocol=protocol,
        station_id="unknown-station",  # placeholder; PipelineManager overwrites
        rssi_dbm=raw.rssi_dbm,
        position=position,
        speed_mps=speed,
        heading_deg=heading,
        drone_timestamp=raw.timestamp,
        received_at=datetime.now(timezone.utc),
        operator_location=None,
        self_id=self_id_text,
    )


__all__ = ["observations_from_frame"]
