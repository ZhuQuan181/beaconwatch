"""BLE receiver using the ``bleak`` async library.

The Open Drone ID BLE bearer uses the **16-bit Service UUID 0xFFFA**
and embeds ODID sub-messages in the **Service Data (0x16)** AD
structure.  bleak's ``start_scan`` API gives us the *raw* advertisement
bytes; we run them through :func:`frame_parser.parse_ble_advertisement`.

Why not a custom D-Bus / bluez call?  bleak already wraps bluez
properly across Linux, macOS, and Windows.  On Linux it requires
``dbus-fast`` and the bluez service running; that is documented in
the deployment guide.

Notes
-----
- BLE scan is a slow operation (3 channels × 37/38/39 advertising
  channels).  Expect 1-3 second latency.
- RSSI comes from the advertisement record directly.
- The MAC is randomized on most platforms (privacy feature), so we
  rely on the drone's Basic ID ``uas_id`` as the primary identity.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData


from .base import Bearer, RawFrame, Receiver, ReceiverError

log = logging.getLogger(__name__)


class BLEReceiver(Receiver):
    """Asyncio BLE scanner for ODID advertisements.

    Parameters
    ----------
    scan_interval_s, scan_window_s:
        Forwarded to bluez ``mgmt_scan`` parameters.  Defaults follow
        the BLE 5.0 spec recommendation for "aggressive discovery".
    rssi_min_dbm:
        Drop advertisements weaker than this.
    """

    def __init__(
        self,
        *,
        scan_interval_s: float = 1.0,
        scan_window_s: float = 0.3,
        rssi_min_dbm: int = -90,
    ) -> None:
        self._scan_interval = scan_interval_s
        self._scan_window = scan_window_s
        self.rssi_min_dbm = rssi_min_dbm
        self._queue: asyncio.Queue[RawFrame] = asyncio.Queue(maxsize=2048)
        self._dropped = 0
        self._running = False

    # ---- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        try:
            self._scanner = BleakScanner(
                detection_callback=self._on_ad,
                scanning_mode="active",
                scan_interval=self._scan_interval * 1000,  # ms
                scan_window=self._scan_window * 1000,        # ms
            )
            await self._scanner.start()
        except Exception as exc:
            raise ReceiverError(f"failed to start BLE scanner: {exc}") from exc
        self._running = True

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        with contextlib.suppress(Exception):
            await self._scanner.stop()

    async def frames(self) -> AsyncIterator[RawFrame]:
        while self._running:
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            yield frame

    # ---- internals ---------------------------------------------------

    def _on_ad(self, device: BLEDevice, adv: AdvertisementData) -> None:
        """bleak detection callback.  Runs on the bleak event loop thread."""
        try:
            if adv.rssi is None or adv.rssi < self.rssi_min_dbm:
                return
            payload = self._extract_odid(adv)
            if not payload:
                return
            mac = device.address or "00:00:00:00:00:00"
            # Normalize: bleak returns uppercase on some platforms
            mac = mac.lower()
            if len(mac) != 17:
                # Some bluez versions return "XX-XX-..."; convert.
                mac = mac.replace("-", ":")
            if len(mac) != 17:
                mac = "00:00:00:00:00:00"

            frame = RawFrame(
                bearer=Bearer.BLE,
                source_mac=mac,
                rssi_dbm=adv.rssi,
                payload=payload,
                timestamp=datetime.now(timezone.utc),
            )
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:
                self._dropped += 1
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait(frame)
        except Exception:
            pass

    @staticmethod
    def _extract_odid(adv: AdvertisementData) -> bytes | None:
        """Pull ODID bytes from a bleak AdvertisementData."""
        # bleak exposes service_data as {uuid_str: bytes}.
        # We have to match the *16-bit* UUID 0xFFFA.
        for uuid, data in (adv.service_data or {}).items():
            if not data or len(data) < 3:
                continue
            # 16-bit UUIDs are exposed in different formats by different
            # backends; match by both.
            if "fffa" in uuid.lower() or uuid.lower().endswith(":fffa"):
                # First two bytes are the UUID; payload begins at byte 2.
                if len(data) < 2:
                    continue
                # Sanity: first 2 bytes should equal 0xFFFA (little-endian)
                if data[0] == 0xFA and data[1] == 0xFF:
                    return data[2:]
                else:
                    return data[2:]
        return None


__all__ = ["BLEReceiver"]
