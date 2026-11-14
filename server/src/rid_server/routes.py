"""FastAPI routes — HTTP + WebSocket."""
from __future__ import annotations

import asyncio
import contextlib
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .bus import ObservationBus
from .db import GeofenceZone, get_session
from .models import (
    AlertOut,
    DroneObservation,
    DroneSummary,
    GeofenceZoneOut,
    Position,
    Station,
)
from .repository import (
    drone_to_summary,
    get_drone,
    list_drones,
    list_observations,
    list_stations,
    list_unidentified_drones,
)

router = APIRouter()


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
@router.get("/alerts", response_model=list[AlertOut])
async def get_alerts(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[AlertOut]:
    """Alerts seen by this process since startup.

    In-memory, not persisted: an alert is a *derived* fact about
    observations that are already stored, so recomputing beats a second
    table that can disagree with the observations.  Surviving restarts
    would need a persisted table; out of scope for a demo build.
    """
    ring = getattr(request.app.state, "alerts", None)
    if ring is None:
        return []
    return [AlertOut(**a) for a in list(ring)[-limit:]]


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
    session: Annotated[AsyncSession, Depends(get_session)],
    since_seconds: Annotated[int | None, Query(ge=0, le=86400)] = None,
) -> list[str]:
    """Drone IDs seen without an operator ID — the non-compliant set.

    Declared BEFORE ``/drones/{drone_id}`` on purpose: FastAPI matches
    routes in declaration order, so a literal path registered after a
    parameterised one is unreachable — "unidentified" would be captured
    as a drone_id and 404 instead.
    """
    since = (
        datetime.now(timezone.utc) - timedelta(seconds=since_seconds)
        if since_seconds is not None
        else None
    )
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
@router.get("/observations", response_model=list[DroneObservation])
async def get_observations(
    session: Annotated[AsyncSession, Depends(get_session)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
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
    parsed_bbox: tuple[float, float, float, float] | None = None
    if bbox is not None:
        try:
            parsed_bbox = tuple(float(x) for x in bbox.split(","))  # type: ignore[assignment]
        except ValueError as e:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, detail=f"invalid bbox: {e}"
            ) from e
    if from_ is not None and to is not None and from_ > to:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail="'from' must be earlier than 'to'",
        )
    since: datetime | None = None
    if since_seconds is not None:
        since = datetime.now(timezone.utc) - timedelta(seconds=since_seconds)
    elif from_ is not None:
        since = from_
    rows = await list_observations(
        session,
        limit=limit,
        offset=offset,
        drone_id=drone_id,
        station_id=station_id,
        bbox=parsed_bbox,
        since=since,
        until=to,
        unidentified_only=unidentified_only,
    )
    return [_row_to_observation(r) for r in rows]


# ── WebSocket: live stream ────────────────────────────────────────────
def _bus_from_app_state(websocket: WebSocket) -> ObservationBus:
    """FastAPI dep that pulls the bus from app.state (set at lifespan startup).

    Defined as a function (not a lambda) so FastAPI's dependency analyzer
    can introspect the parameter type.
    """
    return websocket.app.state.bus


@router.websocket("/ws/observations", name="ws_observations")
async def ws_observations(
    websocket: WebSocket,
    bus: Annotated[ObservationBus, Depends(_bus_from_app_state)],
) -> None:
    """Fan out observations and alerts as JSON frames.

    Protocol (server → client):
        {"type": "obs",   "data": <DroneObservation>}
        {"type": "alert", "data": <Alert>}

    Both ride one connection in arrival order, so a client never sees an
    alert before the observation that caused it.

    The connection is closed by the server if the client sends anything
    other than the optional ``{"type": "ping"}`` heartbeats.
    """
    await websocket.accept()
    async with bus.subscribe() as queue:
        try:
            while True:
                # Race the receive (heartbeat) against the next bus item.
                receive_task = asyncio.create_task(websocket.receive_text())
                get_task = asyncio.create_task(queue.get())
                done, pending = await asyncio.wait(
                    {receive_task, get_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if get_task in done:
                    frame = get_task.result()
                    await websocket.send_json(
                        {"type": frame["kind"], "data": frame["data"]}
                    )
                if receive_task in done:
                    msg = receive_task.result()
                    if msg and msg.strip() != '{"type":"ping"}':
                        await websocket.close(code=1003, reason="no client→server msgs")
                        return
        except WebSocketDisconnect:
            pass
        except Exception:
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
    "/healthz",
    "/metrics",
    "/ws/observations",
    "/docs",
    "/openapi.json",
    "/redoc",
)


def _row_to_observation(r) -> DroneObservation:
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
            accuracy_h_m=0.0,
            accuracy_v_m=0.0,
            speed_mps=r.speed_mps,
            heading_deg=r.heading_deg,
        ),
        drone_timestamp=r.drone_timestamp,
        received_at=r.received_at,
        self_id=r.self_id or "",
    )
