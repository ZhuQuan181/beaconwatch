"""FastAPI routes — HTTP + WebSocket."""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .bus import ObservationBus
from .config import get_settings
from .db import GeofenceZone, Observation, get_session
from .logging_setup import get_logger
from .models import (
    AlertAckIn,
    AlertEventOut,
    AlertOut,
    AlertReopenIn,
    AlertResolveIn,
    DroneObservation,
    DroneSummary,
    GeofenceZoneOut,
    ObservationPage,
    Position,
    Station,
)
from .pagination import decode_cursor, encode_cursor
from .repository import (
    AlertStateConflict,
    acknowledge_alert,
    alert_event_to_out,
    alert_to_out,
    drone_to_summary,
    get_alert,
    get_drone,
    list_alert_events,
    list_alerts,
    list_drones,
    list_observations,
    list_stations,
    list_unidentified_drones,
    reopen_alert,
    resolve_alert,
)
from .security import authorize_websocket

log = get_logger(__name__)

router = APIRouter()
ws_router = APIRouter()


# ── Stations ──────────────────────────────────────────────────────────
@router.get("/stations", response_model=list[Station])
async def get_stations(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[Station]:
    rows = await list_stations(session)
    return [
        Station(
            station_id=r.station_id,
            name=r.name,
            lat=r.lat,
            lon=r.lon,
            alt_m=r.alt_m,
            online=r.online,
            last_seen=r.last_seen,
            firmware_version=r.firmware_version,
            coverage_radius_m=r.coverage_radius_m,
        )
        for r in rows
    ]


# ── Geofence zones ────────────────────────────────────────────────────
@router.get("/zones", response_model=list[GeofenceZoneOut])
async def get_zones(
    session: Annotated[AsyncSession, Depends(get_session)],
) -> list[GeofenceZoneOut]:
    """No-fly volumes the map draws and the rule engine tests against.

    One source of truth: if the red polygon on screen came from a config
    file and the alert came from a DB row, the two would drift and an
    operator would see an alert with no corresponding box.
    """
    rows = (await session.execute(select(GeofenceZone))).scalars().all()
    return [
        GeofenceZoneOut(
            zone_id=r.zone_id,
            name=r.name,
            polygon=r.polygon,
            min_alt_m=r.min_alt_m,
            max_alt_m=r.max_alt_m,
            severity=r.severity,
        )
        for r in rows
    ]


@router.put("/zones/{zone_id}", response_model=GeofenceZoneOut)
async def upsert_zone(
    zone_id: str,
    body: GeofenceZoneOut,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> GeofenceZoneOut:
    """Create or replace a zone.

    Deliberately a PUT with the id in the path (not POST with a
    generated id): demo runs re-seed the same zones, and idempotent
    seeding is worth more here than REST purity.
    """
    if len(body.polygon) < 3:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="polygon needs at least 3 points",
        )
    existing = await session.get(GeofenceZone, zone_id)
    if existing is None:
        session.add(
            GeofenceZone(
                zone_id=zone_id,
                name=body.name,
                polygon=body.polygon,
                min_alt_m=body.min_alt_m,
                max_alt_m=body.max_alt_m,
                severity=body.severity,
            )
        )
    else:
        existing.name = body.name
        existing.polygon = body.polygon
        existing.min_alt_m = body.min_alt_m
        existing.max_alt_m = body.max_alt_m
        existing.severity = body.severity
    await session.commit()
    return GeofenceZoneOut(
        zone_id=zone_id,
        name=body.name,
        polygon=body.polygon,
        min_alt_m=body.min_alt_m,
        max_alt_m=body.max_alt_m,
        severity=body.severity,
    )


# ── Alerts ────────────────────────────────────────────────────────────
def _alert_window(
    from_: datetime | None, to: datetime | None
) -> tuple[datetime | None, datetime | None]:
    since = _as_utc(from_) if from_ is not None else None
    until = _as_utc(to) if to is not None else None
    if since is not None and until is not None and since > until:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="'from' must be earlier than 'to'",
        )
    return since, until


async def _broadcast_alert_update(request: Request, row) -> AlertOut:
    result = alert_to_out(row)
    payload = result.model_dump(mode="json")
    ring = getattr(request.app.state, "alerts", None)
    if ring is not None:
        ring.add(payload)
    bus = getattr(request.app.state, "bus", None)
    if bus is not None:
        try:
            await bus.publish_alert(payload)
        except Exception as exc:  # noqa: BLE001
            log.warning("alert.broadcast.failed", error=str(exc)[:200])
    return result


@router.get("/alerts", response_model=list[AlertOut])
async def get_alerts(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    status_filter: Annotated[
        Literal["active", "open", "acknowledged", "resolved"] | None,
        Query(alias="status", description="Lifecycle state filter"),
    ] = None,
    rule: Annotated[str | None, Query(min_length=1, max_length=32)] = None,
    drone_id: Annotated[str | None, Query(min_length=1, max_length=64)] = None,
    from_: Annotated[
        datetime | None,
        Query(alias="from", description="Triggered-at window start"),
    ] = None,
    to: Annotated[datetime | None, Query()] = None,
) -> list[AlertOut]:
    """Return the durable alert ledger, newest trigger first.

    ``status=active`` means open plus acknowledged.  The other status values
    select exactly one lifecycle state.
    """
    since, until = _alert_window(from_, to)
    rows = await list_alerts(
        session,
        limit=limit,
        status=status_filter,
        rule=rule,
        drone_id=drone_id,
        since=since,
        until=until,
    )
    return [alert_to_out(row) for row in rows]


@router.get("/alerts/{alert_id}", response_model=AlertOut)
async def get_alert_route(
    alert_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AlertOut:
    row = await get_alert(session, alert_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="alert not found")
    return alert_to_out(row)


@router.get(
    "/alerts/{alert_id}/events",
    response_model=list[AlertEventOut],
)
async def get_alert_events_route(
    alert_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AlertEventOut]:
    if await get_alert(session, alert_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="alert not found")
    rows = await list_alert_events(session, alert_id, limit=limit)
    return [alert_event_to_out(row) for row in rows]


@router.post("/alerts/{alert_id}/ack", response_model=AlertOut)
async def acknowledge_alert_route(
    alert_id: str,
    body: AlertAckIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AlertOut:
    """Acknowledge an alert once and return the stored audit record.

    Repeated calls are idempotent: the first operator, timestamp and note
    are never overwritten by a retry.
    """
    try:
        row = await acknowledge_alert(
            session,
            alert_id,
            acknowledged_by=body.acknowledged_by,
            note=body.note,
        )
    except AlertStateConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="alert not found")
    return await _broadcast_alert_update(request, row)


@router.post("/alerts/{alert_id}/resolve", response_model=AlertOut)
async def resolve_alert_route(
    alert_id: str,
    body: AlertResolveIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AlertOut:
    """Resolve an alert and preserve the operator's closing audit record."""
    row = await resolve_alert(
        session,
        alert_id,
        resolved_by=body.resolved_by,
        note=body.note,
    )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="alert not found")
    return await _broadcast_alert_update(request, row)


@router.post("/alerts/{alert_id}/reopen", response_model=AlertOut)
async def reopen_alert_route(
    alert_id: str,
    body: AlertReopenIn,
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> AlertOut:
    """Reopen a resolved alert for another handling cycle."""
    try:
        row = await reopen_alert(
            session,
            alert_id,
            reopened_by=body.reopened_by,
            note=body.note,
        )
    except AlertStateConflict as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="alert not found")
    return await _broadcast_alert_update(request, row)


# ── Drones ────────────────────────────────────────────────────────────
@router.get("/drones", response_model=list[DroneSummary])
async def get_drones(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    online_within_s: Annotated[int, Query(ge=0, le=86400 * 30)] = 60,
) -> list[DroneSummary]:
    rows = await list_drones(session, limit=limit, online_within_s=online_within_s)
    return [drone_to_summary(r) for r in rows]


@router.get("/drones/unidentified", response_model=list[str])
async def get_unidentified_drones(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    since_seconds: Annotated[
        int | None,
        Query(
            ge=1,
            le=86400 * 30,
            description="Lookback window; defaults to the server policy",
        ),
    ] = None,
    all_history: Annotated[
        bool,
        Query(description="Explicitly scan all history (may be expensive)"),
    ] = False,
) -> list[str]:
    """Drone IDs seen without an operator ID — the non-compliant set.

    Declared BEFORE ``/drones/{drone_id}`` on purpose: FastAPI matches
    routes in declaration order, so a literal path registered after a
    parameterised one is unreachable — "unidentified" would be captured
    as a drone_id and 404 instead.

    A finite default window keeps the endpoint from scanning years of
    history on every dashboard poll.  Callers that really need a full
    scan must opt in with ``all_history=true``.
    """
    if all_history and since_seconds is not None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="use either since_seconds or all_history, not both",
        )
    settings = getattr(request.app.state, "settings", None) or get_settings()
    since: datetime | None = None
    if not all_history:
        lookback = (
            since_seconds
            if since_seconds is not None
            else settings.unidentified_lookback_s
        )
        since = datetime.now(timezone.utc) - timedelta(seconds=lookback)
    return list(await list_unidentified_drones(session, since=since))


@router.get("/drones/{drone_id}", response_model=DroneSummary)
async def get_drone_by_id(
    drone_id: str,
    session: Annotated[AsyncSession, Depends(get_session)],
) -> DroneSummary:
    row = await get_drone(session, drone_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="drone not found")
    return drone_to_summary(row)


# ── Observations ──────────────────────────────────────────────────────
def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if bbox is None:
        return None
    try:
        parsed = tuple(float(x) for x in bbox.split(","))
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=f"invalid bbox: {exc}"
        ) from exc
    if len(parsed) != 4:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid bbox")
    min_lon, min_lat, max_lon, max_lat = parsed
    if min_lon > max_lon or min_lat > max_lat:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="bbox minimums must not exceed maximums",
        )
    return min_lon, min_lat, max_lon, max_lat


def _parse_window(
    bbox: str | None,
    since_seconds: int | None,
    from_: datetime | None,
    to: datetime | None,
) -> tuple[
    tuple[float, float, float, float] | None,
    datetime | None,
    datetime | None,
]:
    parsed_bbox = _parse_bbox(bbox)
    normalized_from = _as_utc(from_) if from_ is not None else None
    normalized_to = _as_utc(to) if to is not None else None
    if (
        normalized_from is not None
        and normalized_to is not None
        and normalized_from > normalized_to
    ):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="'from' must be earlier than 'to'",
        )
    if since_seconds is not None:
        since = datetime.now(timezone.utc) - timedelta(seconds=since_seconds)
    else:
        since = normalized_from
    return parsed_bbox, since, normalized_to


async def _read_observation_page(
    session: AsyncSession,
    *,
    limit: int,
    offset: int,
    before: tuple[datetime, str] | None,
    drone_id: str | None,
    station_id: str | None,
    bbox: tuple[float, float, float, float] | None,
    since: datetime | None,
    until: datetime | None,
    unidentified_only: bool,
) -> tuple[list[Observation], str | None]:
    rows = await list_observations(
        session,
        limit=limit + 1,
        offset=offset,
        before=before,
        drone_id=drone_id,
        station_id=station_id,
        bbox=bbox,
        since=since,
        until=until,
        unidentified_only=unidentified_only,
    )
    visible = list(rows[:limit])
    next_cursor = (
        encode_cursor(visible[-1].received_at, visible[-1].observation_id)
        if len(rows) > limit and visible
        else None
    )
    return visible, next_cursor


@router.get("/observations", response_model=list[DroneObservation])
async def get_observations(
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
    cursor: Annotated[
        str | None,
        Query(description="Opaque keyset cursor returned in X-Next-Cursor"),
    ] = None,
    drone_id: Annotated[str | None, Query()] = None,
    station_id: Annotated[str | None, Query()] = None,
    bbox: Annotated[
        str | None,
        Query(
            description="min_lon,min_lat,max_lon,max_lat",
            pattern=r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?$",
        ),
    ] = None,
    since_seconds: Annotated[int | None, Query(ge=0, le=86400)] = None,
    from_: Annotated[
        datetime | None,
        Query(alias="from", description="Absolute window start (ISO 8601, inclusive)"),
    ] = None,
    to: Annotated[
        datetime | None,
        Query(description="Absolute window end (ISO 8601, inclusive)"),
    ] = None,
    unidentified_only: Annotated[
        bool,
        Query(description="Only drones that broadcast no operator ID"),
    ] = False,
) -> list[DroneObservation]:
    if cursor is not None and offset > 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="cursor and offset are mutually exclusive",
        )
    try:
        before = decode_cursor(cursor) if cursor else None
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    parsed_bbox, since, until = _parse_window(bbox, since_seconds, from_, to)
    rows, next_cursor = await _read_observation_page(
        session,
        limit=limit,
        offset=offset,
        before=before,
        drone_id=drone_id,
        station_id=station_id,
        bbox=parsed_bbox,
        since=since,
        until=until,
        unidentified_only=unidentified_only,
    )
    if next_cursor is not None:
        response.headers["X-Next-Cursor"] = next_cursor
    return [_row_to_observation(r) for r in rows]


@router.get("/observations/page", response_model=ObservationPage)
async def get_observations_page(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    cursor: Annotated[
        str | None,
        Query(description="Opaque keyset cursor returned as next_cursor"),
    ] = None,
    drone_id: Annotated[str | None, Query()] = None,
    station_id: Annotated[str | None, Query()] = None,
    bbox: Annotated[
        str | None,
        Query(
            description="min_lon,min_lat,max_lon,max_lat",
            pattern=r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?,-?\d+(\.\d+)?$",
        ),
    ] = None,
    since_seconds: Annotated[int | None, Query(ge=0, le=86400)] = None,
    from_: Annotated[
        datetime | None,
        Query(alias="from", description="Absolute window start (ISO 8601, inclusive)"),
    ] = None,
    to: Annotated[
        datetime | None,
        Query(description="Absolute window end (ISO 8601, inclusive)"),
    ] = None,
    unidentified_only: Annotated[
        bool,
        Query(description="Only drones that broadcast no operator ID"),
    ] = False,
) -> ObservationPage:
    try:
        before = decode_cursor(cursor) if cursor else None
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    parsed_bbox, since, until = _parse_window(bbox, since_seconds, from_, to)
    rows, next_cursor = await _read_observation_page(
        session,
        limit=limit,
        offset=0,
        before=before,
        drone_id=drone_id,
        station_id=station_id,
        bbox=parsed_bbox,
        since=since,
        until=until,
        unidentified_only=unidentified_only,
    )
    return ObservationPage(
        items=[_row_to_observation(r) for r in rows],
        next_cursor=next_cursor,
    )


# ── WebSocket: live stream ────────────────────────────────────────────
def _bus_from_app_state(websocket: WebSocket) -> ObservationBus:
    """FastAPI dep that pulls the bus from app.state (set at lifespan startup).

    Defined as a function (not a lambda) so FastAPI's dependency analyzer
    can introspect the parameter type.
    """
    return websocket.app.state.bus


@ws_router.websocket("/ws/observations", name="ws_observations")
async def ws_observations(
    websocket: WebSocket,
    bus: Annotated[ObservationBus, Depends(_bus_from_app_state)],
) -> None:
    """Fan out observations and alerts as JSON frames.

    Protocol (server → client):
        {"type": "obs",   "data": <DroneObservation>}
        {"type": "alert", "data": <Alert>}
        {"type": "ping"}                    every ws_ping_interval_s

    Clients may send ``{"type":"ping"}``; the server answers with
    ``{"type":"pong"}``.  The periodic server ping lets the browser
    watchdog detect a half-open connection instead of waiting forever for
    ``onclose``.
    """
    allowed, subprotocol = authorize_websocket(websocket)
    if not allowed:
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="unauthorized",
        )
        return

    await websocket.accept(subprotocol=subprotocol)
    settings = getattr(websocket.app.state, "settings", None) or get_settings()
    ping_interval = settings.ws_ping_interval_s
    send_lock = asyncio.Lock()

    async def send(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def sender() -> None:
        async with bus.subscribe() as queue:
            while True:
                frame = await queue.get()
                await send({"type": frame["kind"], "data": frame["data"]})

    async def receiver() -> None:
        while True:
            raw = await websocket.receive_text()
            if len(raw) > 512:
                await websocket.close(
                    code=status.WS_1009_MESSAGE_TOO_BIG,
                    reason="message too large",
                )
                return
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.close(
                    code=status.WS_1007_INVALID_FRAME_PAYLOAD_DATA,
                    reason="invalid JSON",
                )
                return
            if not isinstance(payload, dict):
                await websocket.close(
                    code=status.WS_1003_UNSUPPORTED_DATA,
                    reason="object frame required",
                )
                return
            kind = payload.get("type")
            if kind == "ping":
                await send({"type": "pong"})
            elif kind != "pong":
                await websocket.close(
                    code=status.WS_1003_UNSUPPORTED_DATA,
                    reason="unsupported message type",
                )
                return

    async def heartbeat() -> None:
        while True:
            await asyncio.sleep(ping_interval)
            await send({"type": "ping"})

    tasks = [
        asyncio.create_task(sender(), name="ws-sender"),
        asyncio.create_task(receiver(), name="ws-receiver"),
        asyncio.create_task(heartbeat(), name="ws-heartbeat"),
    ]
    try:
        done, _ = await asyncio.wait(
            tasks,
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                task.result()
    except (WebSocketDisconnect, RuntimeError):
        pass
    except Exception as exc:  # noqa: BLE001
        log.warning("ws.closed_with_error", error=str(exc)[:200])
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            await websocket.close()


# ── Helpers ───────────────────────────────────────────────────────────
# Known route prefixes (for the 404 hint).  We don't introspect the
# router here because that would couple routes.py to a specific
# FastAPI version, and a hand-maintained list is easier to audit.
_KNOWN_PREFIXES: tuple[str, ...] = (
    "/stations",
    "/drones",
    "/observations",
    "/alerts",
    "/healthz",
    "/metrics",
    "/ws/observations",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def _row_to_observation(r) -> DroneObservation:
    raw = r.raw or {}
    raw_position = raw.get("position") or {}
    return DroneObservation(
        observation_id=r.observation_id,
        drone_id=r.drone_id,
        operator_id=r.operator_id,
        station_id=r.station_id,
        protocol=r.protocol,
        rssi_dbm=r.rssi_dbm,
        position=Position(
            lat=r.position_lat,
            lon=r.position_lon,
            alt_m=r.position_alt_m,
            accuracy_h_m=float(raw_position.get("accuracy_h_m") or 0.0),
            accuracy_v_m=float(raw_position.get("accuracy_v_m") or 0.0),
            speed_mps=r.speed_mps,
            heading_deg=r.heading_deg,
        ),
        drone_timestamp=r.drone_timestamp,
        received_at=r.received_at,
        self_id=r.self_id or "",
        operator_location=raw.get("operator_location"),
    )
