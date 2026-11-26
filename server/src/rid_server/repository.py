"""Persistence layer — pure functions, no business logic.

Convert between Pydantic wire models and SQLAlchemy rows.  All DB
write paths (MQTT subscriber, HTTP POST) go through here so we can
add instrumentation / deduplication in one place.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Sequence
from uuid import uuid4

from sqlalchemy import and_, case, delete, func, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from ulid import ULID

from .alerts import Alert
from .audit import AuditContext
from .db import (
    AlertEvent,
    AlertRecord,
    AuditEvent,
    Drone,
    NotificationOutbox,
    Observation,
    Station,
)
from .logging_setup import get_logger
from .models import (
    AlertEventOut,
    AlertOut,
    AuditEventOut,
    DroneObservation,
    DroneSummary,
    Position,
)
from .models import Station as StationModel

log = get_logger(__name__)


class AlertStateConflict(Exception):
    """The requested lifecycle transition is not valid for current state."""


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
    session: AsyncSession,
    station_id: str,
    online: bool = True,
    *,
    commit: bool = True,
) -> None:
    """Refresh ``last_seen``/``online`` for a station, creating it on first sight.

    The first observation from an edge station may arrive *before* the
    station has registered itself.  We auto-create a placeholder row with
    ``(0,0,0)`` coordinates so the FK on ``observations`` is satisfied.
    Deployment-time ``/stations`` registration will overwrite name + lat/lon.

    ``commit=False`` lets the ingest hot path combine this upsert with the
    observation/drone writes in one transaction instead of paying two WAL
    flushes per message.
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
    if commit:
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
def _apply_observation_filters(
    stmt,
    *,
    drone_id: str | None = None,
    station_id: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    unidentified_only: bool = False,
    before: tuple[datetime, str] | None = None,
):
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
    return stmt


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
    stmt = _apply_observation_filters(
        select(Observation),
        drone_id=drone_id,
        station_id=station_id,
        bbox=bbox,
        since=since,
        until=until,
        unidentified_only=unidentified_only,
        before=before,
    ).order_by(
        Observation.received_at.desc(),
        Observation.observation_id.desc(),
    )
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_observations_for_export(
    session: AsyncSession,
    *,
    limit: int,
    drone_id: str | None = None,
    station_id: str | None = None,
    bbox: tuple[float, float, float, float] | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    unidentified_only: bool = False,
) -> Sequence:
    """Select only export columns, deliberately excluding the raw JSON blob.

    A 50k-row export would otherwise materialize 50k full JSON payloads
    that the CSV/GeoJSON writer never reads.
    """

    stmt = _apply_observation_filters(
        select(
            Observation.observation_id,
            Observation.drone_id,
            Observation.operator_id,
            Observation.station_id,
            Observation.protocol,
            Observation.rssi_dbm,
            Observation.position_lat,
            Observation.position_lon,
            Observation.position_alt_m,
            Observation.speed_mps,
            Observation.heading_deg,
            Observation.drone_timestamp,
            Observation.received_at,
            Observation.self_id,
        ),
        drone_id=drone_id,
        station_id=station_id,
        bbox=bbox,
        since=since,
        until=until,
        unidentified_only=unidentified_only,
    ).order_by(
        Observation.received_at.desc(),
        Observation.observation_id.desc(),
    )
    result = await session.execute(stmt.limit(limit))
    return result.all()


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
        .order_by(Observation.drone_id)
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


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
        status=row.status or "open",
        updated_at=_iso(row.updated_at),
        acknowledged_at=_iso(row.acknowledged_at),
        acknowledged_by=row.acknowledged_by,
        acknowledgement_note=row.acknowledgement_note,
        resolved_at=_iso(row.resolved_at),
        resolved_by=row.resolved_by,
        resolution_note=row.resolution_note,
        reopen_count=row.reopen_count or 0,
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
        status="open",
        updated_at=alert.at or None,
    )


def alert_event_to_out(row: AlertEvent) -> AlertEventOut:
    return AlertEventOut(
        event_id=row.event_id,
        alert_id=row.alert_id,
        action=row.action,
        actor=row.actor,
        actor_subject=row.actor_subject,
        request_id=row.request_id,
        note=row.note,
        at=_iso(row.occurred_at) or "",
        detail=row.detail or {},
    )


def _append_alert_event(
    session: AsyncSession,
    alert_id: str,
    *,
    action: str,
    actor: str | None,
    actor_subject: str | None = None,
    request_id: str | None = None,
    note: str | None,
    occurred_at: datetime,
    detail: dict | None = None,
) -> None:
    session.add(
        AlertEvent(
            event_id=uuid4().hex,
            alert_id=alert_id,
            action=action,
            actor=actor,
            actor_subject=actor_subject,
            request_id=request_id,
            note=note,
            occurred_at=occurred_at,
            detail=detail or {},
        )
    )


def audit_event_to_out(row: AuditEvent) -> AuditEventOut:
    return AuditEventOut(
        event_id=row.event_id,
        at=_iso(row.occurred_at) or "",
        request_id=row.request_id,
        actor_subject=row.actor_subject,
        actor_name=row.actor_name,
        actor_roles=list(row.actor_roles or []),
        authenticated=bool(row.authenticated),
        action=row.action,
        resource_type=row.resource_type,
        resource_id=row.resource_id,
        outcome=row.outcome,
        method=row.method,
        path=row.path,
        status_code=row.status_code,
        detail=row.detail or {},
    )


def append_audit_event(
    session: AsyncSession,
    *,
    context: AuditContext,
    occurred_at: datetime,
    outcome: str = "success",
    extra_detail: dict | None = None,
) -> None:
    """Stage one audit row in the caller's transaction.

    Repository functions commit after the mutation and the staged audit
    row, which makes the access record atomic with the business change.
    """

    detail = dict(context.detail)
    if extra_detail:
        detail.update(extra_detail)
    session.add(
        AuditEvent(
            event_id=f"aud_{ULID()}",
            occurred_at=_as_utc(occurred_at),
            request_id=context.request_id,
            actor_subject=context.actor_subject,
            actor_name=context.actor_name,
            actor_roles=list(context.actor_roles),
            authenticated=context.authenticated,
            action=context.action,
            resource_type=context.resource_type,
            resource_id=context.resource_id,
            outcome=outcome,
            method=context.method,
            path=context.path,
            status_code=200,
            detail=detail,
        )
    )


async def list_audit_events(
    session: AsyncSession,
    *,
    limit: int = 100,
    before: tuple[datetime, str] | None = None,
    actor_subject: str | None = None,
    action: str | None = None,
    resource_type: str | None = None,
    resource_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> Sequence[AuditEvent]:
    stmt = select(AuditEvent)
    if before is not None:
        occurred_at, event_id = before
        stmt = stmt.where(
            or_(
                AuditEvent.occurred_at < occurred_at,
                and_(
                    AuditEvent.occurred_at == occurred_at,
                    AuditEvent.event_id < event_id,
                ),
            )
        )
    if actor_subject:
        stmt = stmt.where(AuditEvent.actor_subject == actor_subject)
    if action:
        stmt = stmt.where(AuditEvent.action == action)
    if resource_type:
        stmt = stmt.where(AuditEvent.resource_type == resource_type)
    if resource_id:
        stmt = stmt.where(AuditEvent.resource_id == resource_id)
    if since is not None:
        stmt = stmt.where(AuditEvent.occurred_at >= since)
    if until is not None:
        stmt = stmt.where(AuditEvent.occurred_at <= until)
    stmt = stmt.order_by(
        AuditEvent.occurred_at.desc(),
        AuditEvent.event_id.desc(),
    ).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def _enqueue_notification_safely(
    session: AsyncSession,
    row: AlertRecord,
    *,
    kind: str,
    actor: str | None,
    note: str | None,
    occurred_at: datetime,
    metadata: dict | None = None,
) -> None:
    """Append a webhook delivery without risking the alert transaction.

    Normal operation commits both rows together.  The savepoint means a
    missing pre-migration outbox table (or any enqueue-only failure) rolls
    back just the notification, leaving the durable alert and its audit
    transition intact.
    """
    delivery_id = str(ULID())
    try:
        payload = {
            "schema": "beaconwatch.alert-notification.v1",
            "delivery_id": delivery_id,
            "event": kind,
            "occurred_at": _iso(occurred_at),
            "alert": alert_to_out(row).model_dump(mode="json"),
            "actor": actor,
            "note": note,
            "metadata": metadata or {},
        }
        async with session.begin_nested():
            session.add(
                NotificationOutbox(
                    delivery_id=delivery_id,
                    alert_id=row.alert_id,
                    kind=kind,
                    payload=payload,
                    status="pending",
                    attempts=0,
                    failures=0,
                    next_attempt_at=datetime.now(tz=timezone.utc),
                )
            )
            await session.flush()
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "notification.enqueue.failed",
            alert_id=row.alert_id,
            kind=kind,
            error=str(exc)[:200],
        )


async def _alert_for_update(
    session: AsyncSession, alert_id: str
) -> AlertRecord | None:
    result = await session.execute(
        select(AlertRecord)
        .where(AlertRecord.alert_id == alert_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def _lock_alert_id(session: AsyncSession, alert_id: str) -> None:
    """Serialize alert upserts across API replicas.

    ``SELECT ... FOR UPDATE`` only locks an existing row. Two replicas
    can otherwise both observe "no row" and race to emit the same alert.
    A transaction-scoped PostgreSQL advisory lock closes that gap; SQLite
    tests do not need it and ignore the branch.
    """

    bind = session.get_bind()
    if bind is None or bind.dialect.name != "postgresql":
        return
    digest = hashlib.sha256(alert_id.encode("utf-8")).digest()
    lock_id = int.from_bytes(digest[:8], "big", signed=True)
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": lock_id},
    )


async def upsert_alert(
    session: AsyncSession,
    alert: Alert,
    *,
    now: datetime | None = None,
    enqueue_notification: bool = False,
    dedup_seconds: float | None = None,
) -> AlertRecord | None:
    """Insert an alert, or record a later recurrence of the same alert id.

    ``alert_id`` is deterministic per (drone, rule, zone), so a recurrence
    after the rule dedup window must update the existing operational row
    rather than create a duplicate.  A recurrence after resolution starts a
    new handling cycle, clears the previous cycle's live disposition fields,
    increments ``reopen_count`` and records the transition in the audit log.

    When ``dedup_seconds`` is supplied, the durable ``triggered_at`` value
    is also the cross-replica emission gate.  A candidate inside the window
    returns ``None`` without incrementing the count, writing an event or
    enqueueing a notification.  The check and update run under a
    per-alert advisory lock on PostgreSQL.
    """
    observed_at = _parse_alert_time(
        alert.at, now or datetime.now(tz=timezone.utc)
    )
    await _lock_alert_id(session, alert.alert_id)
    existing = await _alert_for_update(session, alert.alert_id)
    dedup_cutoff = (
        observed_at - timedelta(seconds=max(0.0, dedup_seconds))
        if dedup_seconds is not None
        else None
    )
    if (
        existing is not None
        and dedup_cutoff is not None
        and _as_utc(existing.triggered_at) > dedup_cutoff
    ):
        await session.rollback()
        return None
    previous_status = existing.status if existing is not None else None
    previous_resolution_note = (
        existing.resolution_note if existing is not None else None
    )
    auto_reopen = bool(
        existing is not None
        and existing.status == "resolved"
        and existing.resolved_at is not None
        and _as_utc(existing.resolved_at) < observed_at
    )
    reopen_condition = and_(
        AlertRecord.status == "resolved",
        AlertRecord.resolved_at.is_not(None),
        AlertRecord.resolved_at < observed_at,
    )
    conflict_options: dict = {}
    if dedup_cutoff is not None:
        conflict_options["where"] = AlertRecord.triggered_at <= dedup_cutoff
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
            status="open",
            updated_at=observed_at,
            reopen_count=0,
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
                status=case(
                    (reopen_condition, "open"),
                    else_=AlertRecord.status,
                ),
                updated_at=case(
                    (
                        AlertRecord.updated_at < observed_at,
                        observed_at,
                    ),
                    else_=AlertRecord.updated_at,
                ),
                acknowledged_at=case(
                    (reopen_condition, None),
                    else_=AlertRecord.acknowledged_at,
                ),
                acknowledged_by=case(
                    (reopen_condition, None),
                    else_=AlertRecord.acknowledged_by,
                ),
                acknowledgement_note=case(
                    (reopen_condition, None),
                    else_=AlertRecord.acknowledgement_note,
                ),
                resolved_at=case(
                    (reopen_condition, None),
                    else_=AlertRecord.resolved_at,
                ),
                resolved_by=case(
                    (reopen_condition, None),
                    else_=AlertRecord.resolved_by,
                ),
                resolution_note=case(
                    (reopen_condition, None),
                    else_=AlertRecord.resolution_note,
                ),
                reopen_count=case(
                    (
                        reopen_condition,
                        AlertRecord.reopen_count + 1,
                    ),
                    else_=AlertRecord.reopen_count,
                ),
            ),
            **conflict_options,
        )
    )
    result = await session.execute(stmt)
    if dedup_cutoff is not None and (result.rowcount or 0) == 0:
        await session.rollback()
        return None
    await session.flush()
    row = await session.get(
        AlertRecord, alert.alert_id, populate_existing=True
    )
    if row is None:  # pragma: no cover - only possible after a lost commit
        raise RuntimeError("alert upsert did not produce a row")
    if existing is None:
        _append_alert_event(
            session,
            alert.alert_id,
            action="triggered",
            actor="system",
            note=None,
            occurred_at=observed_at,
            detail={
                "rule": alert.rule,
                "severity": alert.severity,
                "drone_id": alert.drone_id,
            },
        )
    elif auto_reopen:
        _append_alert_event(
            session,
            alert.alert_id,
            action="reopened",
            actor="system",
            note="规则再次触发，已自动重新打开",
            occurred_at=observed_at,
            detail={
                "automatic": True,
                "previous_resolution_note": previous_resolution_note,
            },
        )
    if enqueue_notification:
        kind = "alert.reopened" if auto_reopen else "alert.triggered"
        await _enqueue_notification_safely(
            session,
            row,
            kind=kind,
            actor="system",
            note=None,
            occurred_at=observed_at,
            metadata={
                "recurrence": existing is not None,
                "automatic": auto_reopen,
                "previous_status": previous_status,
                "trigger_count": row.trigger_count,
                "reopen_count": row.reopen_count or 0,
            },
        )
    await session.commit()
    await session.refresh(row)
    return row


def _alert_filters(
    *,
    status: str | None,
    rule: str | None,
    drone_id: str | None,
    since: datetime | None,
    until: datetime | None,
) -> list:
    filters = []
    if status == "active":
        filters.append(AlertRecord.status.in_(("open", "acknowledged")))
    elif status is not None:
        filters.append(AlertRecord.status == status)
    if rule is not None:
        filters.append(AlertRecord.rule == rule)
    if drone_id is not None:
        filters.append(AlertRecord.drone_id == drone_id)
    if since is not None:
        filters.append(AlertRecord.triggered_at >= since)
    if until is not None:
        filters.append(AlertRecord.triggered_at <= until)
    return filters


async def list_alerts(
    session: AsyncSession,
    *,
    limit: int = 50,
    status: str | None = None,
    rule: str | None = None,
    drone_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    before: tuple[datetime, str] | None = None,
) -> Sequence[AlertRecord]:
    stmt = select(AlertRecord).where(
        *_alert_filters(
            status=status,
            rule=rule,
            drone_id=drone_id,
            since=since,
            until=until,
        )
    )
    if before is not None:
        before_at, before_id = before
        stmt = stmt.where(
            or_(
                AlertRecord.triggered_at < before_at,
                and_(
                    AlertRecord.triggered_at == before_at,
                    AlertRecord.alert_id < before_id,
                ),
            )
        )
    stmt = stmt.order_by(
        AlertRecord.triggered_at.desc(),
        AlertRecord.alert_id.desc(),
    ).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def get_alert_stats(
    session: AsyncSession,
    *,
    status: str | None = None,
    rule: str | None = None,
    drone_id: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
) -> dict[str, object]:
    """Aggregate lifecycle, severity and rule counts for one filter set."""
    filters = _alert_filters(
        status=status,
        rule=rule,
        drone_id=drone_id,
        since=since,
        until=until,
    )
    active_filter = AlertRecord.status.in_(("open", "acknowledged"))
    totals = await session.execute(
        select(
            func.count(AlertRecord.alert_id),
            func.sum(
                case((AlertRecord.status == "open", 1), else_=0)
            ),
            func.sum(
                case((AlertRecord.status == "acknowledged", 1), else_=0)
            ),
            func.sum(
                case((AlertRecord.status == "resolved", 1), else_=0)
            ),
            func.min(
                case(
                    (active_filter, AlertRecord.triggered_at),
                    else_=None,
                )
            ),
        ).where(*filters)
    )
    total, open_count, acknowledged, resolved, oldest_active = totals.one()
    severity_rows = await session.execute(
        select(AlertRecord.severity, func.count(AlertRecord.alert_id))
        .where(*filters)
        .group_by(AlertRecord.severity)
    )
    rule_rows = await session.execute(
        select(AlertRecord.rule, func.count(AlertRecord.alert_id))
        .where(*filters)
        .group_by(AlertRecord.rule)
    )
    open_count = int(open_count or 0)
    acknowledged = int(acknowledged or 0)
    resolved = int(resolved or 0)
    return {
        "total": int(total or 0),
        "open": open_count,
        "acknowledged": acknowledged,
        "resolved": resolved,
        "active": open_count + acknowledged,
        "by_severity": {
            str(name): int(count)
            for name, count in severity_rows.all()
        },
        "by_rule": {
            str(name): int(count) for name, count in rule_rows.all()
        },
        "oldest_active_at": (
            _iso(oldest_active)
            if isinstance(oldest_active, datetime)
            else None
        ),
    }


async def purge_expired_resolved_alerts(
    session: AsyncSession,
    *,
    older_than: datetime,
    batch_size: int = 500,
) -> int:
    """Delete one batch of old resolved alerts with no live delivery."""
    pending_delivery = (
        select(NotificationOutbox.delivery_id)
        .where(
            NotificationOutbox.alert_id == AlertRecord.alert_id,
            NotificationOutbox.status.in_(("pending", "delivering")),
        )
        .exists()
    )
    candidates = (
        select(AlertRecord.alert_id)
        .where(
            AlertRecord.status == "resolved",
            AlertRecord.resolved_at.is_not(None),
            AlertRecord.resolved_at < older_than,
            ~pending_delivery,
        )
        .order_by(AlertRecord.resolved_at, AlertRecord.alert_id)
        .limit(batch_size)
    )
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        candidates = candidates.with_for_update(skip_locked=True)
    ids = list((await session.execute(candidates)).scalars().all())
    if not ids:
        return 0

    await session.execute(
        delete(NotificationOutbox).where(
            NotificationOutbox.alert_id.in_(ids)
        )
    )
    await session.execute(
        delete(AlertEvent).where(AlertEvent.alert_id.in_(ids))
    )
    result = await session.execute(
        delete(AlertRecord).where(AlertRecord.alert_id.in_(ids))
    )
    await session.commit()
    return int(result.rowcount or len(ids))


async def purge_expired_notifications(
    session: AsyncSession,
    *,
    older_than: datetime,
    batch_size: int = 500,
) -> int:
    """Delete one batch of terminal notification rows past retention."""
    terminal_at = func.coalesce(
        NotificationOutbox.delivered_at,
        NotificationOutbox.updated_at,
        NotificationOutbox.created_at,
    )
    candidates = (
        select(NotificationOutbox.delivery_id)
        .where(
            NotificationOutbox.status.in_(("delivered", "dead")),
            terminal_at < older_than,
        )
        .order_by(
            terminal_at,
            NotificationOutbox.delivery_id,
        )
        .limit(batch_size)
    )
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        candidates = candidates.with_for_update(skip_locked=True)
    ids = list((await session.execute(candidates)).scalars().all())
    if not ids:
        return 0
    result = await session.execute(
        delete(NotificationOutbox).where(
            NotificationOutbox.delivery_id.in_(ids)
        )
    )
    await session.commit()
    return int(result.rowcount or len(ids))


async def get_alert(
    session: AsyncSession, alert_id: str
) -> AlertRecord | None:
    return await session.get(AlertRecord, alert_id)


async def list_alert_events(
    session: AsyncSession,
    alert_id: str,
    *,
    limit: int = 100,
) -> Sequence[AlertEvent]:
    stmt = (
        select(AlertEvent)
        .where(AlertEvent.alert_id == alert_id)
        .order_by(
            AlertEvent.occurred_at.desc(),
            AlertEvent.event_id.desc(),
        )
        .limit(limit)
    )
    result = await session.execute(stmt)
    return list(reversed(result.scalars().all()))


async def notification_outbox_counts(
    session: AsyncSession,
) -> dict[str, int]:
    """Return outbox state and cumulative attempt counters for metrics."""
    counts = {
        "pending": 0,
        "delivering": 0,
        "delivered": 0,
        "dead": 0,
        "attempts": 0,
        "failures": 0,
    }
    status_rows = await session.execute(
        select(NotificationOutbox.status, func.count()).group_by(
            NotificationOutbox.status
        )
    )
    for status, count in status_rows.all():
        if status in counts:
            counts[status] = int(count)
    totals = await session.execute(
        select(
            func.coalesce(func.sum(NotificationOutbox.attempts), 0),
            func.coalesce(func.sum(NotificationOutbox.failures), 0),
        )
    )
    attempts, failures = totals.one()
    counts["attempts"] = int(attempts or 0)
    counts["failures"] = int(failures or 0)
    return counts


async def acknowledge_alert(
    session: AsyncSession,
    alert_id: str,
    *,
    acknowledged_by: str,
    note: str = "",
    now: datetime | None = None,
    enqueue_notification: bool = False,
    audit: AuditContext | None = None,
) -> AlertRecord | None:
    """Record the first acknowledgement for an alert.

    The conditional UPDATE makes concurrent acknowledgements safe: only
    one request can transition the row from unacknowledged to
    acknowledged, and every retry returns that same audit record.
    """
    row = await _alert_for_update(session, alert_id)
    if row is None:
        return None
    if row.status == "resolved":
        raise AlertStateConflict(
            "resolved alerts must be reopened before acknowledgement"
        )
    if row.acknowledged_at is None:
        transition_at = now or datetime.now(tz=timezone.utc)
        transition_at = _as_utc(transition_at)
        actor = acknowledged_by.strip()[:128]
        clean_note = note.strip()[:1000] or None
        previous_status = row.status
        row.acknowledged_at = transition_at
        row.acknowledged_by = actor
        row.acknowledgement_note = clean_note
        row.status = "acknowledged"
        row.updated_at = transition_at
        _append_alert_event(
            session,
            alert_id,
            action="acknowledged",
            actor=actor,
            actor_subject=audit.actor_subject if audit else None,
            request_id=audit.request_id if audit else None,
            note=clean_note,
            occurred_at=transition_at,
        )
        if enqueue_notification:
            await _enqueue_notification_safely(
                session,
                row,
                kind="alert.acknowledged",
                actor=actor,
                note=clean_note,
                occurred_at=transition_at,
                metadata={"previous_status": previous_status},
            )
        if audit is not None:
            append_audit_event(
                session,
                context=audit,
                occurred_at=transition_at,
                extra_detail={"changed": True},
            )
        await session.commit()
        await session.refresh(row)
    elif audit is not None:
        append_audit_event(
            session,
            context=audit,
            occurred_at=datetime.now(tz=timezone.utc),
            outcome="noop",
            extra_detail={"changed": False},
        )
        await session.commit()
    return row


async def resolve_alert(
    session: AsyncSession,
    alert_id: str,
    *,
    resolved_by: str,
    note: str = "",
    now: datetime | None = None,
    enqueue_notification: bool = False,
    audit: AuditContext | None = None,
) -> AlertRecord | None:
    """Resolve an alert.  Repeating the same call is idempotent."""
    row = await _alert_for_update(session, alert_id)
    if row is None:
        return None
    if row.resolved_at is not None:
        if audit is not None:
            append_audit_event(
                session,
                context=audit,
                occurred_at=datetime.now(tz=timezone.utc),
                outcome="noop",
                extra_detail={"changed": False},
            )
            await session.commit()
        return row

    transition_at = _as_utc(now or datetime.now(tz=timezone.utc))
    actor = resolved_by.strip()[:128]
    clean_note = note.strip()[:1000] or None
    previous_status = row.status
    row.resolved_at = transition_at
    row.resolved_by = actor
    row.resolution_note = clean_note
    row.status = "resolved"
    row.updated_at = transition_at
    _append_alert_event(
        session,
        alert_id,
        action="resolved",
        actor=actor,
        actor_subject=audit.actor_subject if audit else None,
        request_id=audit.request_id if audit else None,
        note=clean_note,
        occurred_at=transition_at,
    )
    if enqueue_notification:
        await _enqueue_notification_safely(
            session,
            row,
            kind="alert.resolved",
            actor=actor,
            note=clean_note,
            occurred_at=transition_at,
            metadata={"previous_status": previous_status},
        )
    if audit is not None:
        append_audit_event(
            session,
            context=audit,
            occurred_at=transition_at,
            extra_detail={"changed": True},
        )
    await session.commit()
    await session.refresh(row)
    return row


async def reopen_alert(
    session: AsyncSession,
    alert_id: str,
    *,
    reopened_by: str,
    note: str = "",
    now: datetime | None = None,
    enqueue_notification: bool = False,
    audit: AuditContext | None = None,
) -> AlertRecord | None:
    """Reopen a resolved alert and start a fresh acknowledgement cycle."""
    row = await _alert_for_update(session, alert_id)
    if row is None:
        return None
    if row.status != "resolved":
        raise AlertStateConflict("only resolved alerts can be reopened")

    transition_at = _as_utc(now or datetime.now(tz=timezone.utc))
    actor = reopened_by.strip()[:128]
    clean_note = note.strip()[:1000] or None
    previous_status = row.status
    previous_resolution_note = row.resolution_note
    row.status = "open"
    row.acknowledged_at = None
    row.acknowledged_by = None
    row.acknowledgement_note = None
    row.resolved_at = None
    row.resolved_by = None
    row.resolution_note = None
    row.reopen_count = (row.reopen_count or 0) + 1
    row.updated_at = transition_at
    _append_alert_event(
        session,
        alert_id,
        action="reopened",
        actor=actor,
        actor_subject=audit.actor_subject if audit else None,
        request_id=audit.request_id if audit else None,
        note=clean_note,
        occurred_at=transition_at,
        detail={
            "automatic": False,
            "previous_resolution_note": previous_resolution_note,
        },
    )
    if enqueue_notification:
        await _enqueue_notification_safely(
            session,
            row,
            kind="alert.reopened",
            actor=actor,
            note=clean_note,
            occurred_at=transition_at,
            metadata={
                "automatic": False,
                "previous_status": previous_status,
                "reopen_count": row.reopen_count,
            },
        )
    if audit is not None:
        append_audit_event(
            session,
            context=audit,
            occurred_at=transition_at,
            extra_detail={"changed": True},
        )
    await session.commit()
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
