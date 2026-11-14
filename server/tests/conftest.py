"""Server-side test fixtures.

All DB-touching tests use an **in-memory SQLite** instance.

PostGIS ``Geometry`` columns can't be auto-created on plain SQLite
(SpatiaLite extension isn't available), so the test environment uses
a custom :class:`...` schema-builder that drops the spatial DDL hooks.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

import rid_server.db as db_mod
from rid_server.app import create_app
from rid_server.bus import ObservationBus
from rid_server.config import Settings
from rid_server.db import Base
from rid_server.mqtt_subscriber import MqttSubscriber


# ── Disable geoalchemy2 SQLite DDL hooks ────────────────────────────────
# The library auto-registers a Table.after_create listener that calls
# RecoverGeometryColumn() / CreateSpatialIndex() on every Geometry
# column.  In production we use SpatiaLite on top of TimescaleDB, but
# in unit tests we just need the column to be a plain TEXT field for
# assertion convenience.  We monkey-patch the sqlite-side hooks to
# no-ops, which keeps the listener signatures intact but harmless.
import geoalchemy2.admin  # noqa: E402  (ensure the listeners are registered)
import geoalchemy2.admin.dialects.sqlite as _ga_sqlite  # noqa: E402

_ga_sqlite.after_create = lambda *a, **kw: None
_ga_sqlite.before_create = lambda *a, **kw: None
_ga_sqlite.before_drop = lambda *a, **kw: None
_ga_sqlite.after_drop = lambda *a, **kw: None


# ── Settings override ─────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _sqlite_settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Force all DB code to use an in-memory SQLite per test."""
    from rid_server import config as cfg

    # Build a fresh Settings (bypass the lru_cache from a prior import)
    cfg.get_settings.cache_clear()
    s = Settings(
        db_host="ignored",
        db_port=0,
        db_name=":memory:",
        mqtt_host="test",
        mqtt_port=1883,
        log_level="WARNING",
    )
    # Replace the cached singleton
    monkeypatch.setattr(cfg, "get_settings", lambda: s)
    return s


# ── Per-test async DB ─────────────────────────────────────────────────
@pytest_asyncio.fixture
async def db_engine():
    """Create a fresh in-memory SQLite, build schema, dispose on teardown."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Point rid_server.db module at THIS engine (so get_session() uses it)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    db_mod._engine = engine
    db_mod._session_factory = factory
    try:
        yield engine
    finally:
        await engine.dispose()
        db_mod._engine = None
        db_mod._session_factory = None


@pytest_asyncio.fixture
async def session(db_engine) -> AsyncIterator[AsyncSession]:
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as s:
        yield s


# ── App + bus for HTTP / WebSocket tests ──────────────────────────────
@pytest_asyncio.fixture
async def app_with_bus(db_engine):
    """FastAPI app wired up with a real DB engine + in-process bus."""
    bus = ObservationBus()
    subscriber = MqttSubscriber(_sqlite_settings)
    # Don't actually start the subscriber — the HTTP tests just need
    # the bus available via app.state.
    app = create_app()
    app.state.bus = bus
    app.state.subscriber = subscriber
    yield app, bus
    await subscriber.stop()


# ── Sample observation builders ───────────────────────────────────────
def make_obs(
    *,
    drone_id: str = "drone-A",
    station_id: str = "station-1",
    lat: float = 37.7749,
    lon: float = -122.4194,
    alt_m: float = 120.0,
    protocol: str = "wifi_nan",
    received_at: datetime | None = None,
    rssi_dbm: int | None = -62,
) -> dict[str, Any]:
    if received_at is None:
        received_at = datetime.now(timezone.utc)
    return {
        "observation_id": f"obs-{drone_id}-{received_at.isoformat()}",
        "drone_id": drone_id,
        "operator_id": None,
        "station_id": station_id,
        "protocol": protocol,
        "rssi_dbm": rssi_dbm,
        "position": {
            "lat": lat,
            "lon": lon,
            "alt_m": alt_m,
            "accuracy_h_m": 5.0,
            "accuracy_v_m": 8.0,
            "speed_mps": 5.0,
            "heading_deg": 180.0,
        },
        "drone_timestamp": received_at.isoformat(),
        "received_at": received_at.isoformat(),
        "self_id": "",
        "operator_location": None,
    }


# ── Quiet down noisy loggers during tests ─────────────────────────────
@pytest.fixture(autouse=True)
def _quiet_loggers():
    for name in ("uvicorn", "aiomqtt", "sqlalchemy.engine"):
        logging.getLogger(name).setLevel(logging.ERROR)
    yield
