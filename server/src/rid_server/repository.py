"""Persistence layer — pure functions, no business logic.

Convert between Pydantic wire models and SQLAlchemy rows.  All DB
write paths (MQTT subscriber, HTTP POST) go through here so we can
add instrumentation / deduplication in one place.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db import Drone, Observation, Station
from .models import DroneObservation, DroneSummary, Position
from .models import Station as StationModel


# ── Station ───────────────────────────────────────────────────────────
async def upsert_station(session: AsyncSession, station: StationModel) -> None:
    """Insert or refresh a station from a trusted heartbeat."""
    stmt = (
        pg_insert(Station)
        .values(
            station_id=station.station_id,
            name=station.name,
            lat=station.lat,
            lon=station.lon,
            alt_m=station.alt_m,
            firmware_version=station.firmware_version,
            last_seen=station.last_seen or datetime.now(tz=timezone.utc),
            online=station.online,
        )
        .on_conflict_do_update(
            index_elements=["station_id"],
            set_=dict(
                name=station.name,
                lat=station.lat,
                lon=station.lon,
                alt_m=station.alt_m,
                firmware_version=station.firmware_version,
                last_seen=station.last_seen or datetime.now(tz=timezone.utc),
                online=station.online,
            ),
        )
    )
    await session.execute(stmt)
    await session.commit()


async def touch_station(
    session: AsyncSession, station_id: str, online: bool = True
) -> None:
    """Refresh ``last_seen``/``online`` for a station, creating it on first sight.

    The first observation from an edge station may arrive *before* the
    station has registered itself.  We auto-create a placeholder row with
    ``(0,0,0)`` coordinates so the FK on ``observations`` is satisfied.
    Deployment-time ``/stations`` registration will overwrite name + lat/lon.
    """
    now = datetime.now(tz=timezone.utc)
    stmt = (
        pg_insert(Station)
        .values(
            station_id=station_id,
            name=station_id,    # placeholder; real name via /stations POST
            lat=0.0,
            lon=0.0,
            alt_m=0.0,
            last_seen=now,
            online=online,
        )
        .on_conflict_do_update(
            index_elements=["station_id"],
            set_=dict(last_seen=now, online=online),
        )
    )
    await session.execute(stmt)
    await session.commit()


async def list_stations(session: AsyncSession) -> Sequence[Station]:
    result = await session.execute(select(Station).order_by(Station.station_id))
    return result.scalars().all()


# ── Observation → DB ──────────────────────────────────────────────────
async def write_observation(
    session: AsyncSession, obs: DroneObservation
) -> None:
    """Append an observation, upserting the parent ``drones`` row.

    Uses an UPSERT on ``drones`` to keep ``last_seen`` and
    ``observation_count`` monotonic.  The per-row observation is
    inserted with ``ON CONFLICT DO NOTHING`` on ``observation_id``
    (idempotency — edge may retry after reconnect).
    """
    # SQLite path: write lat/lon directly.  When the column is PostGIS
    # ``Geometry`` in the future, swap to ``from_shape(Point(...), srid=4326)``
    # and remove the explicit lat/lon columns.
    # 1) Upsert drone summary FIRST so the FK exists when we insert
    # the observation row.  ``on_conflict_do_nothing`` on observations
    # means we may hit a duplicate path later, but the drone row is
    # always authoritative.
    drone_stmt = (
        pg_insert(Drone)
        .values(
            drone_id=obs.drone_id,
            uas_id=obs.drone_id,    # we don't carry the decoded uas_id on the wire yet
            ua_type="unknown",
            first_seen=obs.received_at,
            last_seen=obs.received_at,
            last_position_lat=obs.position.lat,
            last_position_lon=obs.position.lon,
            last_position_alt_m=obs.position.alt_m,
            last_station_id=obs.station_id,
            observation_count=1,
        )
        .on_conflict_do_update(
            index_elements=["drone_id"],
            set_=dict(
                last_seen=obs.received_at,
                last_position_lat=obs.position.lat,
                last_position_lon=obs.position.lon,
                last_position_alt_m=obs.position.alt_m,
                last_station_id=obs.station_id,
                observation_count=Drone.observation_count + 1,
            ),
        )
    )
    await session.execute(drone_stmt)

    # 2) Append the observation row itself.  Idempotent on
    # (observation_id, received_at) so edge retries are safe.
    obs_stmt = (
        pg_insert(Observation)
        .values(
            observation_id=obs.observation_id,
            drone_id=obs.drone_id,
            station_id=obs.station_id,
            protocol=obs.protocol,
            received_at=obs.received_at,
            drone_timestamp=obs.drone_timestamp,
            position_lat=obs.position.lat,
            position_lon=obs.position.lon,
            position_alt_m=obs.position.alt_m,
            speed_mps=obs.position.speed_mps,
            heading_deg=obs.position.heading_deg,
            rssi_dbm=obs.rssi_dbm,
            self_id=obs.self_id or "",
            operator_id=obs.operator_id,
            raw=obs.model_dump(mode="json"),
        )
        .on_conflict_do_nothing(index_elements=["observation_id", "received_at"])
    )
    obs_result = await session.execute(obs_stmt)
    inserted = obs_result.rowcount > 0
    if not inserted:
        # Duplicate — the drone counter was already incremented above.
        # Roll back the counter bump so totals stay accurate.
        await session.execute(
            update(Drone)
            .where(Drone.drone_id == obs.drone_id)
            .values(observation_count=Drone.observation_count - 1)
        )
    await session.commit()


# ── Reads ─────────────────────────────────────────────────────────────
async def list_observations(
    session: AsyncSession,
    *,
    limit: int = 100,
    offset: int = 0,
    drone_id: str | None = None,
    station_id: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    unidentified_only: bool = False,
) -> Sequence[Observation]:
    """Filtered, time-ordered (newest first) observation listing.

    Filters compose with AND.  ``bbox`` is ``(min_lon, min_lat, max_lon, max_lat)``.

    ``since``/``until`` are an absolute window, which is what a
    timeline scrubber needs.  ``since_seconds`` relative queries can't
    express "show me 14:32:10 to 14:35:00 yesterday".

    ``unidentified_only`` keeps only rows with no operator ID — the
    "find the ghost drone" view.
    """
    stmt = select(Observation).order_by(Observation.received_at.desc())
    if drone_id is not None:
        stmt = stmt.where(Observation.drone_id == drone_id)
    if station_id is not None:
        stmt = stmt.where(Observation.station_id == station_id)
    if since is not None:
        stmt = stmt.where(Observation.received_at >= since)
    if until is not None:
        stmt = stmt.where(Observation.received_at <= until)
    if unidentified_only:
        stmt = stmt.where(Observation.operator_id.is_(None))
    if bbox is not None:
        min_lon, min_lat, max_lon, max_lat = bbox
        stmt = stmt.where(
            Observation.position_lon >= min_lon,
            Observation.position_lon <= max_lon,
            Observation.position_lat >= min_lat,
            Observation.position_lat <= max_lat,
        )
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_unidentified_drones(
    session: AsyncSession,
    *,
    since: datetime | None = None,
) -> Sequence[str]:
    """Distinct drone IDs that never broadcast an operator ID.

    These are the interesting ones for a RID deployment: a drone that
    transmits a UAS ID but no operator ID is either non-compliant or
    privacy-mode, and either way it's the thing an operator should look
    at first.
    """
    stmt = (
        select(Observation.drone_id)
        .where(Observation.operator_id.is_(None))
        .distinct()
    )
    if since is not None:
        stmt = stmt.where(Observation.received_at >= since)
    result = await session.execute(stmt)
    return [r[0] for r in result.all()]


async def list_drones(
    session: AsyncSession,
    *,
    limit: int = 200,
    online_within_s: int = 60,
) -> Sequence[Drone]:
    """Return drones, with an in-memory ``is_online`` filter.

    We pre-compute the cutoff once; SQL-side ``now() - interval`` would
    also work but is harder to mock in tests.
    """
    cutoff = datetime.now(tz=timezone.utc).timestamp() - online_within_s
    cutoff_dt = datetime.fromtimestamp(cutoff, tz=timezone.utc)
    stmt = (
        select(Drone)
        .where(Drone.last_seen >= cutoff_dt)
        .order_by(Drone.last_seen.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_drone(
    session: AsyncSession, drone_id: str
) -> Drone | None:
    return await session.get(Drone, drone_id)


# ── Mappers ───────────────────────────────────────────────────────────
def drone_to_summary(d: Drone) -> DroneSummary:
    pos = Position(
        lat=d.last_position_lat,
        lon=d.last_position_lon,
        alt_m=d.last_position_alt_m,
        accuracy_h_m=0.0,
        accuracy_v_m=0.0,
        speed_mps=0.0,
        heading_deg=0.0,
    )
    return DroneSummary(
        drone_id=d.drone_id,
        uas_id=d.uas_id,
        ua_type=d.ua_type,
        first_seen=d.first_seen,
        last_seen=d.last_seen,
        last_position=pos,
        last_station_id=d.last_station_id,
        observation_count=d.observation_count,
    )
