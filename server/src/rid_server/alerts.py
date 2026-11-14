"""Alert rules: geofence breach, altitude limit, unidentified drone.

Rules run after a successful ingest, so an alert always corresponds to
a row that's actually in the database — an alert you can't click back to
is worse than no alert.

Design notes that matter:

* **Point-in-polygon uses ray casting on a closed ring.**  The ring is
  stored in GeoJSON order ([lon, lat], no duplicate closing point), so
  we close it ourselves.  Nothing here assumes convexity — L-shaped
  restricted zones are common around airports.

* **Alerts are deduplicated per (drone, rule, zone) within a window.**
  A drone hovering inside a no-fly zone emits an alert every second at
  4 Hz.  Without this, one drone buries the whole panel in a minute.

* **`unidentified` is a rule, not a status.**  A drone that broadcasts
  no operator ID is the headline case for a RID deployment: everything
  compliant is visible to everyone, so the interesting target is the one
  that isn't.  It fires once per drone per session, not per frame.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

Severity = Literal["info", "warn", "critical"]


@dataclass
class Zone:
    zone_id: str
    name: str
    polygon: list[list[float]]  # [[lon, lat], ...]
    min_alt_m: float | None = None
    max_alt_m: float | None = None
    severity: Severity = "warn"


@dataclass
class Alert:
    alert_id: str
    rule: str
    severity: Severity
    drone_id: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)
    at: str = ""  # ISO timestamp of the offending observation


def point_in_polygon(lon: float, lat: float, ring: list[list[float]]) -> bool:
    """Ray casting.  Returns True when (lon, lat) is inside `ring`.

    The ring is treated as implicitly closed.  Points exactly on an edge
    are not guaranteed either way — acceptable here, since a fix sitting
    on a boundary within GPS noise is indistinguishable from inside.
    """
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        # Does the horizontal ray at `lat` cross this edge?
        if (yi > lat) != (yj > lat):
            # x coordinate of the crossing
            t = (lat - yi) / (yj - yi) if yj != yi else 0.0
            x_cross = xi + t * (xj - xi)
            if lon < x_cross:
                inside = not inside
        j = i
    return inside


def _distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


class AlertEngine:
    """Evaluates rules against one observation at a time.

    Stateful only for deduplication; everything else is a pure function
    of (observation, zones).  That keeps the rules testable without a
    broker or a database.
    """

    def __init__(
        self,
        *,
        max_alt_m: float | None = 120.0,
        unidentified: bool = True,
        dedup_seconds: float = 120.0,
        clock=time.monotonic,
    ) -> None:
        self.max_alt_m = max_alt_m
        self.track_unidentified = unidentified
        self.dedup_seconds = dedup_seconds
        self._clock = clock
        # (drone_id, rule, key) -> last time this alert fired
        self._last_fired: dict[tuple[str, str, str], float] = {}

    # ── Public ────────────────────────────────────────────────────
    def evaluate(self, obs, zones: Iterable[Zone] = ()) -> list[Alert]:
        """Return the alerts this observation triggers (possibly none)."""
        out: list[Alert] = []
        at = _iso(obs.received_at)
        lat = obs.position.lat
        lon = obs.position.lon
        alt = obs.position.alt_m

        # 1) Geofence breach — position inside a restricted volume.
        for z in zones:
            if not point_in_polygon(lon, lat, z.polygon):
                continue
            in_band = (
                (z.min_alt_m is None or alt >= z.min_alt_m)
                and (z.max_alt_m is None or alt <= z.max_alt_m)
            )
            if not in_band:
                continue
            a = self._make(
                obs,
                rule="geofence",
                key=z.zone_id,
                severity=z.severity,
                message=f"{obs.drone_id} 进入禁飞区「{z.name}」",
                detail={
                    "zone_id": z.zone_id,
                    "zone_name": z.name,
                    "alt_m": round(alt, 1),
                    "lat": lat,
                    "lon": lon,
                },
                at=at,
            )
            if a:
                out.append(a)

        # 2) Altitude limit — global ceiling, independent of geography.
        if self.max_alt_m is not None and alt > self.max_alt_m:
            a = self._make(
                obs,
                rule="altitude",
                key="global",
                severity="warn",
                message=f"{obs.drone_id} 超过限高 {alt:.0f} m（上限 {self.max_alt_m:.0f} m）",
                detail={"alt_m": round(alt, 1), "limit_m": self.max_alt_m},
                at=at,
            )
            if a:
                out.append(a)

        # 3) Unidentified — no operator ID broadcast at all.
        if self.track_unidentified and not obs.operator_id:
            a = self._make(
                obs,
                rule="unidentified",
                key="session",
                severity="critical",
                message=f"{obs.drone_id} 未广播操作员 ID",
                detail={"uas_id": obs.drone_id, "alt_m": round(alt, 1)},
                at=at,
                # Fires once per drone for the whole session: this is a
                # property of the target, not of the moment.
                dedup_seconds=None,
            )
            if a:
                out.append(a)

        return out

    # ── Internals ─────────────────────────────────────────────────
    def _make(
        self,
        obs,
        *,
        rule: str,
        key: str,
        severity: Severity,
        message: str,
        detail: dict[str, Any],
        at: str,
        dedup_seconds: float | None = -1,
    ) -> Alert | None:
        """Build an alert, or None when it's a duplicate inside the window.

        `dedup_seconds=-1` means "use the engine default"; None means
        "never repeat for this key".
        """
        window = self.dedup_seconds if dedup_seconds == -1 else dedup_seconds
        dedup_key = (obs.drone_id, rule, key)
        now = self._clock()
        prev = self._last_fired.get(dedup_key)
        if prev is not None:
            if window is None:
                return None  # once per session, ever
            if now - prev < window:
                return None
        self._last_fired[dedup_key] = now
        return Alert(
            alert_id=f"{rule}-{obs.drone_id}-{abs(hash(dedup_key)) % 10**8}",
            rule=rule,
            severity=severity,
            drone_id=obs.drone_id,
            message=message,
            detail=detail,
            at=at,
        )

    def reset(self) -> None:
        """Forget dedup state (tests, and on a long idle period)."""
        self._last_fired.clear()


def _iso(dt) -> str:
    if dt is None:
        return ""
    return dt.isoformat()


class AlertRing:
    """Bounded in-memory history of recent alerts.

    Alerts are derived facts about stored observations, so keeping only
    the last N in memory is honest: the source of truth is the
    observations table, and a restart simply re-derives from live data.
    A bounded ring also means a long-running demo can't leak memory.
    """

    def __init__(self, capacity: int = 200) -> None:
        self.capacity = capacity
        self._items: list[dict[str, Any]] = []

    def add(self, alert: Alert) -> None:
        self._items.append(alert.__dict__)
        if len(self._items) > self.capacity:
            del self._items[: len(self._items) - self.capacity]

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def clear(self) -> None:
        self._items.clear()


def distance_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Public alias — used by the coverage-gap check."""
    return _distance_m(lat1, lon1, lat2, lon2)
