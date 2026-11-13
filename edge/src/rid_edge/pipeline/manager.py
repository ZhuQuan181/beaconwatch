"""High-level pipeline: RawFrame → filtered RidObservation stream.

Wraps :func:`observations_from_frame` with:

- per-(drone, station) deduplication (drop retransmits of the same BasicID
  within a configurable time window)
- per-station rate limiting (drop bursts above ``max_obs_per_sec``)
- station_id stamping (the underlying :func:`observations_from_frame`
  doesn't know the station id, so we inject it here)

This is the only public entry point that the MQTT publisher and the
WebSocket server should use.
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict, deque
from collections.abc import Iterator
from threading import Lock
from typing import Any

from ..common.dedup import Deduper
from ..models import RidObservation
from ..receivers.base import RawFrame
from .parser import observations_from_frame

log = logging.getLogger(__name__)


class PipelineManager:
    """Per-station pipeline that turns frames into publish-ready observations.

    Parameters
    ----------
    station_id
        Filled into every emitted :class:`RidObservation.station_id`.
    dedup_window_s
        Drop retransmits of the same (drone_id, observation_id) within
        this many seconds.
    max_obs_per_sec
        Soft cap on observations emitted per second.  Excess is dropped
        (logged at DEBUG).
    """

    def __init__(
        self,
        *,
        station_id: str,
        dedup_window_s: float = 5.0,
        max_obs_per_sec: int = 50,
    ) -> None:
        if not station_id:
            raise ValueError("station_id is required")
        self.station_id = station_id
        self.deduper = Deduper(window_s=dedup_window_s)
        self.max_obs_per_sec = max_obs_per_sec

        # Rate-limit bookkeeping
        self._ratelock = Lock()
        self._rate_window: deque[float] = deque(maxlen=max_obs_per_sec * 2)

    # ------------------------------------------------------------------
    def _accept_rate(self) -> bool:
        """Return True if we can emit another observation this second."""
        with self._ratelock:
            now = time.monotonic()
            # Drop entries older than 1 second
            while self._rate_window and (now - self._rate_window[0]) > 1.0:
                self._rate_window.popleft()
            if len(self._rate_window) >= self.max_obs_per_sec:
                return False
            self._rate_window.append(now)
            return True

    # ------------------------------------------------------------------
    def process(self, raw: RawFrame) -> list[RidObservation]:
        """Run one frame through the full pipeline.

        Returns the list of observations to publish (possibly empty).
        """
        out: list[RidObservation] = []
        for obs in observations_from_frame(raw):
            # Stamp the station id (parser doesn't know it).
            obs = obs.model_copy(update={"station_id": self.station_id})
            if self.deduper.seen(obs.drone_id, obs.observation_id):
                continue
            if not self._accept_rate():
                log.debug("rate-limited obs for drone %s", obs.drone_id)
                continue
            out.append(obs)
        return out

    def process_many(self, frames: Iterator[RawFrame]) -> list[RidObservation]:
        """Convenience: process an iterator of frames at once."""
        out: list[RidObservation] = []
        for f in frames:
            out.extend(self.process(f))
        return out


__all__ = ["PipelineManager"]
