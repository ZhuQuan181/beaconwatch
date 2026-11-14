"""Opaque keyset cursors for observation history.

Offset pagination gets slower as the history grows and can skip/duplicate
rows when new observations arrive between page requests.  The cursor is a
base64url-encoded ``(received_at, observation_id)`` tuple, not a secret.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone


def encode_cursor(received_at: datetime, observation_id: str) -> str:
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    payload = {
        "received_at": received_at.astimezone(timezone.utc).isoformat(),
        "observation_id": observation_id,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> tuple[datetime, str]:
    """Decode a cursor or raise ``ValueError`` for malformed input."""
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        received_at = datetime.fromisoformat(str(payload["received_at"]))
        observation_id = payload["observation_id"]
        if not isinstance(observation_id, str) or not observation_id:
            raise ValueError("cursor observation_id is invalid")
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        return received_at.astimezone(timezone.utc), observation_id
    except (ValueError, TypeError, KeyError, json.JSONDecodeError, UnicodeDecodeError,
            binascii.Error) as exc:
        raise ValueError("invalid cursor") from exc
