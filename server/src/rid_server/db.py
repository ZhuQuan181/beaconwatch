"""SQLAlchemy 2.0 ORM + async engine.

Notes
-----
* Uses **plain lat/lon Float columns** instead of PostGIS ``Geometry`` so
  the same model works on bare PostgreSQL, TimescaleDB, and the in-memory
  SQLite we use for tests.  Bbox queries are still fast at our expected
  cardinality with the composite ``ix_observations_lat_lon`` index.
* Bbox distance filter in :mod:`repository` uses a `BETWEEN AND` pair
  rather than ``ST_DWithin`` for portability.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
)
from .config import get_settings


class Base(DeclarativeBase):
    """Project-wide declarative base."""


class Station(Base):
    __tablename__ = "stations"

    station_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    lat: Mapped[float] = mapped_column(Float, nullable=False)
    lon: Mapped[float] = mapped_column(Float, nullable=False)
    alt_m: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    online: Mapped[bool] = mapped_column(default=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    firmware_version: Mapped[str] = mapped_column(String(32), default="")
    # Receiver reach in metres.  Drives the coverage circle on the map
    # and lets an operator see gaps between sites.  Default matches the
    # 1-5 km envelope typical of a Wi-Fi/BLE RID receiver.
    coverage_radius_m: Mapped[float] = mapped_column(
        Float, nullable=False, default=3000.0, server_default="3000"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    # Spatial index: stations are mostly read by location
    __table_args__ = (
        Index("ix_stations_geom", "lat", "lon"),
    )


class GeofenceZone(Base):
    """A no-fly / restricted volume that triggers alerts.

    Stored as an ordered [lon, lat] ring so the API, the rule engine and
    the map all consume identical geometry — a config file plus a DB row
    would drift, and then the red box on screen wouldn't match the alert.
    """

    __tablename__ = "geofence_zones"

    zone_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    polygon: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), nullable=False
    )
    min_alt_m: Mapped[float | None] = mapped_column(Float)
    max_alt_m: Mapped[float | None] = mapped_column(Float)
    severity: Mapped[str] = mapped_column(
        String(16), nullable=False, default="warn", server_default="warn"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class Drone(Base):
    __tablename__ = "drones"

    drone_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    uas_id: Mapped[str] = mapped_column(String(64), nullable=False)
    ua_type: Mapped[str] = mapped_column(String(32), default="unknown")
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_position_lat: Mapped[float] = mapped_column(Float, nullable=False)
    last_position_lon: Mapped[float] = mapped_column(Float, nullable=False)
    last_position_alt_m: Mapped[float] = mapped_column(Float, nullable=False)
    last_station_id: Mapped[str] = mapped_column(String(64), nullable=False)
    observation_count: Mapped[int] = mapped_column(BigInteger, default=0)


class Observation(Base):
    """One row per received ``RidObservation`` from the edge."""

    __tablename__ = "observations"

    observation_id: Mapped[str] = mapped_column(
        String(64), primary_key=True
    )
    # NOTE: TimescaleDB requires the partitioning column
    # (``received_at``) to participate in **every** unique
    # constraint, so the PK is a composite of
    # ``(observation_id, received_at)`` rather than just
    # ``observation_id``.  Inserts always carry the timestamp, so
    # the wider key has no practical cost.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
        primary_key=True,
    )
    drone_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("drones.drone_id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    station_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("stations.station_id", ondelete="SET NULL"),
        nullable=False, index=True,
    )
    protocol: Mapped[str] = mapped_column(String(32), nullable=False)
    drone_timestamp: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    position_lat: Mapped[float] = mapped_column(Float, nullable=False)
    position_lon: Mapped[float] = mapped_column(Float, nullable=False)
    position_alt_m: Mapped[float] = mapped_column(Float, nullable=False)
    speed_mps: Mapped[float] = mapped_column(Float, default=0.0)
    heading_deg: Mapped[float] = mapped_column(Float, default=0.0)
    # Signal strength at the receiving station.  The edge has always
    # captured this; it used to be dropped on ingest, which made
    # "which station hears this drone best" unanswerable.
    rssi_dbm: Mapped[int | None] = mapped_column(Integer)
    self_id: Mapped[str] = mapped_column(String(256), default="")
    operator_id: Mapped[str | None] = mapped_column(String(64))
    # JSONB on PostgreSQL, JSON on SQLite (tests).
    # SQL column is ``raw`` (created by Alembic 0001_initial).  We expose it
    # under the attribute name ``raw`` so ORM and DB agree.
    raw: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"), default=dict
    )

    __table_args__ = (
        Index(
            "ix_observations_drone_received",
            "drone_id",
            "received_at",
        ),
        Index(
            "ix_observations_lat_lon",
            "position_lat",
            "position_lon",
        ),
    )


# ── Engine / session factory ──────────────────────────────────────────
_engine = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine():
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            pool_size=settings.db_pool_max,
            max_overflow=settings.db_pool_min,
            pool_pre_ping=True,
            echo=False,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            get_engine(), expire_on_commit=False
        )
    return _session_factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
