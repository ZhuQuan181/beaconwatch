"""Persistence layer — pure functions, no business logic.

Convert between Pydantic wire models and SQLAlchemy rows.  All DB
write paths (MQTT subscriber, HTTP POST) go through here so we can
add instrumentation / deduplication in one place.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .alerts import Alert
from .db import AlertRecord, Drone, Observation, Station
from .models import AlertOut, DroneObservation, DroneSummary, Position
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
) -> bool:
    """Append an observation, upserting the parent ``drones`` row.

    The observation insert is the source of truth for whether the
    aggregate count increments.  A placeholder drone row is created
    first to satisfy the foreign key; only a newly inserted observation
    then updates the summary.  This removes the old
    increment-then-compensate race for duplicate MQTT deliveries.

    ``last_seen``/position fields only move forward, so an out-of-order
    retry cannot make a drone appear to jump backwards.

    Returns ``True`` when a new observation row was inserted.
    """
    # SQLite path: write lat/lon directly.  When the column is PostGIS
    # ``Geometry`` in the future, swap to ``from_shape(Point(...), srid=4326)``
    # and remove the explicit lat/lon columns.
    # 1) Ensure the FK target exists.  This is intentionally not the
    # aggregate update: a duplicate observation must not increment it.
    await session.execute(
        pg_insert(Drone)
        .values(
            drone_id=obs.drone_id,
            uas_id=obs.drone_id,    # decoded uas_id is not on the wire yet
            ua_type="unknown",
            first_seen=obs.received_at,
            last_seen=obs.received_at,
            last_position_lat=obs.position.lat,
            last_position_lon=obs.position.lon,
            last_position_alt_m=obs.position.alt_m,
            last_station_id=obs.station_id,
            observation_count=0,
        )
        .on_conflict_do_nothing(index_elements=["drone_id"])
    )

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
    inserted = (obs_result.rowcount or 0) > 0

    if inserted:
        # Only a row that actually landed contributes to the aggregate.
        # CASE expressions are portable across PostgreSQL and SQLite.
        should_advance = Drone.last_seen <= obs.received_at
        drone_upsert = (
            pg_insert(Drone)
            .values(
                drone_id=obs.drone_id,
                uas_id=obs.drone_id,
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
                    first_seen=case(
                        (Drone.first_seen <= obs.received_at, Drone.first_seen),
                        else_=obs.received_at,
                    ),
                    last_seen=case(
                        (should_advance, obs.received_at),
                        else_=Drone.last_seen,
                    ),
                    last_position_lat=case(
                        (should_advance, obs.position.lat),
                        else_=Drone.last_position_lat,
                    ),
                    last_position_lon=case(
                        (should_advance, obs.position.lon),
                        else_=Drone.last_position_lon,
                    ),
                    last_position_alt_m=case(
                        (should_advance, obs.position.alt_m),
                        else_=Drone.last_position_alt_m,
                    ),
                    last_station_id=case(
                        (should_advance, obs.station_id),
                        else_=Drone.last_station_id,
                    ),
                    observation_count=Drone.observation_count + 1,
                ),
            )
        )
        await session.execute(drone_upsert)
    await session.commit()
    return inserted


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
    before: tuple[datetime, str] | None = None,
) -> Sequence[Observation]:
    """Filtered, time-ordered (newest first) observation listing.

    Filters compose with AND.  ``bbox`` is ``(min_lon, min_lat, max_lon, max_lat)``.

    ``since``/``until`` are an absolute window, which is what a
    timeline scrubber needs.  ``since_seconds`` relative queries can't
    express "show me 14:32:10 to 14:35:00 yesterday".

    ``unidentified_only`` keeps only rows with no operator ID — the
    "find the ghost drone" view.
    """
    stmt = select(Observation).order_by(
        Observation.received_at.desc(),
        Observation.observation_id.desc(),
    )
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
    if before is not None:
        before_at, before_id = before
        stmt = stmt.where(
            or_(
                Observation.received_at < before_at,
                and_(
                    Observation.received_at == before_at,
                    Observation.observation_id < before_id,
                ),
            )
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


# ── Alerts ────────────────────────────────────────────────────────────
def _parse_alert_time(value: str, fallback: datetime) -> datetime:
    if not value:
        return fallback
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def alert_to_out(row: AlertRecord) -> AlertOut:
    """Map a durable alert row to the public wire shape."""
    return AlertOut(
        alert_id=row.alert_id,
        rule=row.rule,
        severity=row.severity,
        drone_id=row.drone_id,
        message=row.message,
        detail=row.detail or {},
        at=_iso(row.triggered_at) or "",
        first_seen_at=_iso(row.first_seen_at),
        trigger_count=row.trigger_count,
        acknowledged_at=_iso(row.acknowledged_at),
        acknowledged_by=row.acknowledged_by,
        acknowledgement_note=row.acknowledgement_note,
    )


def transient_alert_to_out(alert: Alert) -> AlertOut:
    """Wire shape used only when persisting an alert fails."""
    return AlertOut(
        alert_id=alert.alert_id,
        rule=alert.rule,
        severity=alert.severity,
        drone_id=alert.drone_id,
        message=alert.message,
        detail=alert.detail,
        at=alert.at,
        first_seen_at=alert.at or None,
        trigger_count=1,
    )


async def upsert_alert(
    session: AsyncSession,
    alert: Alert,
    *,
    now: datetime | None = None,
) -> AlertRecord:
    """Insert an alert, or record a later recurrence of the same alert id.

    ``alert_id`` is deterministic per (drone, rule, zone), so a recurrence
    after the rule dedup window must update the existing operational row
    rather than create a duplicate.  The first observation time and any
    acknowledgement are deliberately preserved.
    """
    observed_at = _parse_alert_time(
        alert.at, now or datetime.now(tz=timezone.utc)
    )
    stmt = (
        pg_insert(AlertRecord)
        .values(
            alert_id=alert.alert_id,
            rule=alert.rule,
            severity=alert.severity,
            drone_id=alert.drone_id,
            message=alert.message[:512],
            detail=alert.detail,
            first_seen_at=observed_at,
            triggered_at=observed_at,
            trigger_count=1,
        )
        .on_conflict_do_update(
            index_elements=["alert_id"],
            set_=dict(
                rule=alert.rule,
                severity=alert.severity,
                drone_id=alert.drone_id,
                message=alert.message[:512],
                detail=alert.detail,
                first_seen_at=case(
                    (
                        AlertRecord.first_seen_at > observed_at,
                        observed_at,
                    ),
                    else_=AlertRecord.first_seen_at,
                ),
                triggered_at=case(
                    (
                        AlertRecord.triggered_at <= observed_at,
                        observed_at,
                    ),
                    else_=AlertRecord.triggered_at,
                ),
                trigger_count=AlertRecord.trigger_count + 1,
            ),
        )
    )
    await session.execute(stmt)
    await session.commit()
    row = await session.get(AlertRecord, alert.alert_id)
    if row is None:  # pragma: no cover - only possible after a lost commit
        raise RuntimeError("alert upsert did not produce a row")
    await session.refresh(row)
    return row


async def list_alerts(
    session: AsyncSession, *, limit: int = 50
) -> Sequence[AlertRecord]:
    stmt = (
        select(AlertRecord)
        .order_by(
            AlertRecord.triggered_at.desc(),
            AlertRecord.alert_id.desc(),
        )
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def acknowledge_alert(
    session: AsyncSession,
    alert_id: str,
    *,
    acknowledged_by: str,
    note: str = "",
    now: datetime | None = None,
) -> AlertRecord | None:
    """Record the first acknowledgement for an alert.

    The conditional UPDATE makes concurrent acknowledgements safe: only
    one request can transition the row from unacknowledged to
    acknowledged, and every retry returns that same audit record.
    """
    row = await session.get(AlertRecord, alert_id)
    if row is None:
        return None
    if row.acknowledged_at is None:
        await session.execute(
            update(AlertRecord)
            .where(
                AlertRecord.alert_id == alert_id,
                AlertRecord.acknowledged_at.is_(None),
            )
            .values(
                acknowledged_at=now or datetime.now(tz=timezone.utc),
                acknowledged_by=acknowledged_by.strip()[:128],
                acknowledgement_note=note.strip()[:1000] or None,
            )
        )
        await session.commit()
        row = await session.get(AlertRecord, alert_id)
        if row is None:  # pragma: no cover
            return None
        await session.refresh(row)
    return row


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
