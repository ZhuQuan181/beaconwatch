"""Receiver protocol: convert raw bytes into :class:`RawFrame` objects.

A *RawFrame* is the lowest common denominator between Wi-Fi and BLE:
it carries the bearer type, the broadcaster MAC, RSSI, and the raw
ODID message-pack payload (already extracted from the 802.11 vendor
IE or the BLE service-data section).

Receivers produce RawFrames; the parser turns them into RidObservations.
This separation lets us test parsers against synthetic frames without
running any radio.
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator


class Bearer(str, enum.Enum):
    """Physical layer that produced the frame."""

    WIFI_NAN = "wifi_nan"
    BLE = "ble"


@dataclass(slots=True, frozen=True)
class RawFrame:
    """A raw ODID-bearing frame extracted from a single radio packet.

    Attributes
    ----------
    bearer:
        Which physical layer delivered this frame.
    source_mac:
        MAC address of the broadcaster (6 bytes, colon-separated lowercase).
    rssi_dbm:
        Received signal strength in dBm (negative integer).
    payload:
        The ODID message-pack or single-message bytes — i.e. the bytes
        *after* the 802.11 vendor IE / BLE service-data envelope.
    timestamp:
        UTC time the frame was captured (defaults to "now").
    channel:
        Wi-Fi channel (if known); 0 otherwise.
    """

    bearer: Bearer
    source_mac: str
    rssi_dbm: int
    payload: bytes
    timestamp: datetime = datetime.now(timezone.utc)
    channel: int = 0
    ts_monotonic: float = 0.0

    def __post_init__(self) -> None:
        # MAC normalization
        if not isinstance(self.source_mac, str):
            raise TypeError("source_mac must be a str")
        if len(self.source_mac) != 17:
            raise ValueError(f"source_mac must be 17 chars (xx:xx:xx:xx:xx:xx): got {self.source_mac!r}")
        if not (-150 <= self.rssi_dbm <= 0):
            raise ValueError(f"rssi_dbm out of range: {self.rssi_dbm}")
        # Allow empty payload (synthetic test frames).  Real receivers
        # never emit one; the parser discards them.


class Receiver(abc.ABC):
    """Async stream of :class:`RawFrame` from a single radio bearer.

    Implementations must be safe to ``start()`` and ``stop()`` from
    different tasks.  They must not raise on benign errors (bad CRC,
    decryption failure, OS-level buffer overflow); instead they should
    log and continue.
    """

    @abc.abstractmethod
    async def start(self) -> None:
        """Open the radio / DBus connection."""

    @abc.abstractmethod
    async def stop(self) -> None:
        """Release the radio / DBus connection."""

    @abc.abstractmethod
    def frames(self) -> AsyncIterator[RawFrame]:
        """Yield frames until :meth:`stop` is called.

        Must be an async generator (or anything ``async for`` accepts).
        """
        raise NotImplementedError
        yield  # pragma: no cover  (makes this a generator)


class ReceiverError(RuntimeError):
    """A receiver failed in a way that needs operator attention (e.g. the
    Wi-Fi monitor-mode interface disappeared)."""


def parse_mac(b: bytes) -> str:
    """Convert 6 raw bytes to ``xx:xx:xx:xx:xx:xx`` lowercase."""
    if len(b) != 6:
        raise ValueError(f"MAC must be 6 bytes, got {len(b)}")
    return ":".join(f"{x:02x}" for x in b)


def format_rssi(dbm: int) -> str:
    return f"{dbm}dBm"


__all__ = ["Bearer", "RawFrame", "Receiver", "ReceiverError", "parse_mac"]
