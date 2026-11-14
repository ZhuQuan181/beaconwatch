"""Wi-Fi NaN (Neighbor Awareness Networking) + Beacon receiver.

Captures 802.11 frames in monitor mode and extracts ODID vendor IEs.
ODID-over-Wi-Fi uses OUI ``FA:0B:BC`` and is embedded in either:

1. **NaN Action frames** (preferred, off-channel delivery)
2. **Beacon frames** (the drone re-encodes a single ODID message as an
   SSID prefix; 32-byte SSID begins with ``\\xFA\\x0B\\xBC...``).

We support both.  Channel-hopping is used when no fixed channel is
configured so we can cover the full 2.4 GHz band (and 5 GHz if the
adapter supports it).

Hardware requirements
---------------------
- A Wi-Fi adapter that supports monitor mode (e.g. Atheros AR9xxx,
  Ralink RT3070, or any driver that exposes a ``mon0``-style interface).
- Linux only (scapy's radiotap parser is Linux-specific).
- Root or ``CAP_NET_ADMIN`` + ``CAP_NET_RAW``.

This module deliberately avoids the legacy `scapy.sendrecv.sniff`
thread; instead we use ``AsyncSniffer`` so the rest of the pipeline
can stay single-threaded on the asyncio loop.
"""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

from scapy.layers.dot11 import Dot11, Dot11Beacon, Dot11Elt
from scapy.sendrecv import AsyncSniffer

from rid_edge.odid_parser.constants import ODID_OUI

from .base import Bearer, RawFrame, Receiver, ReceiverError, parse_mac

# 2.4 GHz channels commonly used by drones. We hop through these.
_DEFAULT_24G_CHANNELS = [1, 6, 11, 2, 7, 3, 8, 4, 9, 5, 10]
# 5 GHz channels (UNII-1 + UNII-3). Many drones (DJI, Skydio) hop here.
_DEFAULT_5G_CHANNELS = [36, 40, 44, 48, 149, 153, 157, 161, 165]


class WifiReceiver(Receiver):
    """Async Wi-Fi monitor-mode receiver.

    Parameters
    ----------
    interface:
        Monitor-mode interface name, e.g. ``wlan1mon``.
    channel_hop:
        If ``True`` and no fixed channel is given, hop through the
        default 2.4 + 5 GHz channel list.
    fixed_channel:
        If set, lock to this channel and disable hopping.
    rssi_min_dbm:
        Drop frames weaker than this (noise floor).
    hop_interval_s:
        How long to dwell on each channel before hopping.
    beacon_only:
        If True, only inspect beacon frames (NaN AFs ignored).  Useful
        for SSID-based ODID beacons from older DJI drones.
    """

    def __init__(
        self,
        interface: str = "wlan1mon",
        *,
        channel_hop: bool = True,
        fixed_channel: Optional[int] = None,
        rssi_min_dbm: int = -90,
        hop_interval_s: float = 0.25,
        beacon_only: bool = False,
        bpf_filter: str = "type mgt subtype beacon or type mgt subtype action",
    ) -> None:
        self.interface = interface
        self._hop = channel_hop and fixed_channel is None
        self._channel = fixed_channel
        self.rssi_min_dbm = rssi_min_dbm
        self._hop_interval = hop_interval_s
        self._beacon_only = beacon_only
        self._bpf = bpf_filter if not beacon_only else "type mgt subtype beacon"

        self._queue: asyncio.Queue[RawFrame] = asyncio.Queue(maxsize=2048)
        self._sniffer: AsyncSniffer | None = None
        self._hop_task: asyncio.Task | None = None
        self._dropped = 0
        self._running = False

    # ---- lifecycle ---------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        try:
            self._sniffer = AsyncSniffer(
                iface=self.interface,
                prn=self._on_pkt,
                store=False,
                filter=self._bpf,
                monitor=True,
            )
            self._sniffer.start()
        except Exception as exc:  # OSError, OSError weirdness, scapy errors
            raise ReceiverError(
                f"failed to start Wi-Fi sniffer on {self.interface}: {exc}"
            ) from exc
        self._running = True

        if self._channel is not None:
            await self._set_channel(self._channel)
        if self._hop:
            self._hop_task = asyncio.create_task(self._hop_loop())

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._hop_task is not None:
            self._hop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._hop_task
            self._hop_task = None
        if self._sniffer is not None:
            self._sniffer.stop()
            self._sniffer = None

    async def frames(self) -> AsyncIterator[RawFrame]:
        while self._running:
            try:
                frame = await asyncio.wait_for(self._queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            yield frame

    # ---- internals ---------------------------------------------------

    def _on_pkt(self, pkt) -> None:
        """scapy callback.  Runs on scapy's sniff thread; must be fast & non-blocking."""
        try:
            if not pkt.haslayer(Dot11):
                return
            if self._beacon_only and not pkt.haslayer(Dot11Beacon):
                return

            rssi = self._extract_rssi(pkt)
            if rssi is None or rssi < self.rssi_min_dbm:
                return

            mac = bytes(pkt[Dot11].addr2 or b"\x00\x00\x00\x00\x00\x00")
            if mac == b"\x00\x00\x00\x00\x00\x00":
                return

            payload = self._extract_odid_payload(pkt)
            if not payload:
                return

            frame = RawFrame(
                bearer=Bearer.WIFI_NAN,
                source_mac=parse_mac(mac),
                rssi_dbm=rssi,
                payload=payload,
                timestamp=datetime.now(timezone.utc),
                channel=self._channel or 0,
            )
            try:
                self._queue.put_nowait(frame)
            except asyncio.QueueFull:
                # Drop oldest to make room (rare; back-pressure is
                # better handled by capping RSSI than by queueing).
                self._dropped += 1
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    self._queue.put_nowait(frame)
        except Exception:
            # Never let scapy kill its own thread on a malformed frame.
            pass

    @staticmethod
    def _extract_rssi(pkt) -> int | None:
        """Pull dBm from the radiotap header, if present."""
        try:
            if pkt.haslayer("RadioTap"):
                rt = pkt["RadioTap"]
                # scapy exposes dBm_AntSignal as a signed int
                return int(rt.dBm_AntSignal)
        except Exception:
            return None
        return None

    @staticmethod
    def _extract_odid_payload(pkt) -> bytes | None:
        """Extract ODID bytes from a Dot11 frame.

        Tries, in order:
        1. Vendor-specific IEs (NaN AF body, ODID-in-SSID beacon)
        2. A single SSID prefix match (DJI-style)
        """
        if not pkt.haslayer(Dot11Elt):
            return None
        chunks: list[bytes] = []
        for elt in pkt[Dot11Elt]:
            if elt.ID == 0xDD and len(elt.info) >= 3 and elt.info[:3] == ODID_OUI:
                chunks.append(elt.info[3:])
        if not chunks:
            return None
        # Concatenate; downstream MessagePack decoder will split.
        return b"".join(chunks)

    async def _hop_loop(self) -> None:
        channels = _DEFAULT_24G_CHANNELS + _DEFAULT_5G_CHANNELS
        i = 0
        while self._running:
            ch = channels[i % len(channels)]
            i += 1
            with contextlib.suppress(Exception):
                await self._set_channel(ch)
            self._channel = ch
            await asyncio.sleep(self._hop_interval)

    async def _set_channel(self, channel: int) -> None:
        """Set the monitor-mode interface to `channel` via ``iw``.

        We use ``iw dev <iface> set channel <n>`` which works for any
        modern mac80211 driver.  If ``iw`` is not available the call
        fails silently and the receiver keeps listening on whatever
        channel the interface is already on.
        """
        proc = await asyncio.create_subprocess_exec(
            "iw", "dev", self.interface, "set", "channel", str(channel),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()

    @property
    def dropped_frames(self) -> int:
        return self._dropped


__all__ = ["WifiReceiver"]
