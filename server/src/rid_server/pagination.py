"""Opaque keyset cursors for history endpoints.

Offset pagination gets slower as the history grows and can skip/duplicate
rows when new observations arrive between page requests.  The cursor is a
base64url-encoded ``(received_at, observation_id)`` tuple, not a secret.
"""
from __future__ import annotations

import base64
import binascii
import json
from datetime import datetime, timezone


def _encode_payload(payload: dict) -> str:
    raw = json.dumps(
        payload, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_payload(value: str) -> dict:
    try:
        padded = value + "=" * (-len(value) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("cursor payload must be an object")
        return payload
    except (
        ValueError,
        TypeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        binascii.Error,
    ) as exc:
        raise ValueError("invalid cursor") from exc


def encode_cursor(received_at: datetime, observation_id: str) -> str:
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    payload = {
        "received_at": received_at.astimezone(timezone.utc).isoformat(),
        "observation_id": observation_id,
    }
    return _encode_payload(payload)


def decode_cursor(value: str) -> tuple[datetime, str]:
    """Decode a cursor or raise ``ValueError`` for malformed input."""
    try:
        payload = _decode_payload(value)
        received_at_value = payload["received_at"]
        if not isinstance(received_at_value, str):
            raise ValueError("cursor received_at is invalid")
        received_at = datetime.fromisoformat(received_at_value)
        observation_id = payload["observation_id"]
        if not isinstance(observation_id, str) or not observation_id:
            raise ValueError("cursor observation_id is invalid")
        if received_at.tzinfo is None:
            received_at = received_at.replace(tzinfo=timezone.utc)
        return received_at.astimezone(timezone.utc), observation_id
    except (
        ValueError,
        TypeError,
        KeyError,
    ) as exc:
        raise ValueError("invalid cursor") from exc


def encode_alert_cursor(triggered_at: datetime, alert_id: str) -> str:
    if triggered_at.tzinfo is None:
        triggered_at = triggered_at.replace(tzinfo=timezone.utc)
    payload = {
        "kind": "alert",
        "v": 1,
        "triggered_at": triggered_at.astimezone(timezone.utc).isoformat(),
        "alert_id": alert_id,
    }
    return _encode_payload(payload)


def decode_alert_cursor(value: str) -> tuple[datetime, str]:
    """Decode an alert cursor, rejecting observation or foreign cursors."""
    try:
        payload = _decode_payload(value)
        if payload.get("kind") != "alert" or payload.get("v") != 1:
            raise ValueError("invalid alert cursor")
        triggered_at_value = payload["triggered_at"]
        alert_id = payload["alert_id"]
        if not isinstance(triggered_at_value, str):
            raise ValueError("invalid alert cursor")
        if not isinstance(alert_id, str) or not alert_id:
            raise ValueError("invalid alert cursor")
        triggered_at = datetime.fromisoformat(triggered_at_value)
        if triggered_at.tzinfo is None:
            triggered_at = triggered_at.replace(tzinfo=timezone.utc)
        return triggered_at.astimezone(timezone.utc), alert_id
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError("invalid alert cursor") from exc
