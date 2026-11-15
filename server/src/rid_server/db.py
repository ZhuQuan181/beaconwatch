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
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
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


class AlertRecord(Base):
    """Persistent alert ledger with an operator workflow trail.

    Rule evaluation is still an in-process, observation-derived operation,
    but the resulting alert, its current lifecycle state and every operator
    transition are operational records.  They must survive a process restart
    and remain queryable for an audit, unlike the old bounded in-memory ring.
    """

    __tablename__ = "alerts"

    alert_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    rule: Mapped[str] = mapped_column(String(32), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    drone_id: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(String(512), nullable=False)
    detail: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=dict,
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    trigger_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="open",
        server_default="open",
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    acknowledged_by: Mapped[str | None] = mapped_column(String(128))
    acknowledgement_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    resolved_by: Mapped[str | None] = mapped_column(String(128))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    reopen_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('open', 'acknowledged', 'resolved')",
            name="ck_alerts_status",
        ),
        Index("ix_alerts_triggered_alert", "triggered_at", "alert_id"),
        Index("ix_alerts_drone_id", "drone_id"),
        Index("ix_alerts_acknowledged_at", "acknowledged_at"),
        Index("ix_alerts_status_triggered", "status", "triggered_at"),
        Index("ix_alerts_status_resolved", "status", "resolved_at"),
        Index("ix_alerts_drone_triggered", "drone_id", "triggered_at"),
        Index("ix_alerts_rule_triggered", "rule", "triggered_at"),
        Index("ix_alerts_severity_triggered", "severity", "triggered_at"),
    )


class AlertEvent(Base):
    """Immutable operator/system transition history for an alert."""

    __tablename__ = "alert_events"

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    alert_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("alerts.alert_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str | None] = mapped_column(String(128))
    actor_subject: Mapped[str | None] = mapped_column(String(128))
    request_id: Mapped[str | None] = mapped_column(String(128))
    note: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    detail: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=dict,
    )

    __table_args__ = (
        Index(
            "ix_alert_events_alert_occurred",
            "alert_id",
            "occurred_at",
        ),
        Index("ix_alert_events_actor_subject", "actor_subject"),
    )


class AuditEvent(Base):
    """Security/access audit for state-changing operator API calls.

    Alert lifecycle events remain in ``alert_events`` because they are part
    of the alert domain.  This table provides a cross-resource, append-only
    record of who invoked which mutation, with the request id and effective
    roles.  Rows are written in the same transaction as the mutation.
    """

    __tablename__ = "audit_events"

    event_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    request_id: Mapped[str | None] = mapped_column(String(128))
    actor_subject: Mapped[str | None] = mapped_column(String(128))
    actor_name: Mapped[str | None] = mapped_column(String(128))
    actor_roles: Mapped[list] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=list,
    )
    authenticated: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="false"
    )
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(32), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    outcome: Mapped[str] = mapped_column(
        String(16), nullable=False, default="success", server_default="success"
    )
    method: Mapped[str] = mapped_column(String(8), nullable=False)
    path: Mapped[str] = mapped_column(String(512), nullable=False)
    status_code: Mapped[int] = mapped_column(
        Integer, nullable=False, default=200, server_default="200"
    )
    detail: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=dict,
    )

    __table_args__ = (
        CheckConstraint(
            "outcome IN ('success', 'noop')",
            name="ck_audit_events_outcome",
        ),
        Index("ix_audit_events_occurred_event", "occurred_at", "event_id"),
        Index(
            "ix_audit_events_actor_occurred",
            "actor_subject",
            "occurred_at",
        ),
        Index(
            "ix_audit_events_resource_occurred",
            "resource_type",
            "resource_id",
            "occurred_at",
        ),
        Index("ix_audit_events_action_occurred", "action", "occurred_at"),
    )


class NotificationOutbox(Base):
    """Durable webhook delivery queue for alert lifecycle events.

    The row is written in the same transaction as the alert transition.
    A separate worker claims due rows with a lease, posts them to the
    configured webhook and records the result.  A crash after a successful
    HTTP response can therefore produce a duplicate, never a lost event;
    receivers should de-duplicate on ``delivery_id``.
    """

    __tablename__ = "notification_outbox"

    delivery_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    alert_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("alerts.alert_id", ondelete="CASCADE"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(
        JSONB().with_variant(JSON(), "sqlite"),
        nullable=False,
        default=dict,
    )
    status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
        server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    lease_id: Mapped[str | None] = mapped_column(String(64))
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    delivered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'dead')",
            name="ck_notification_outbox_status",
        ),
        Index(
            "ix_notification_outbox_status_next_attempt",
            "status",
            "next_attempt_at",
        ),
        Index(
            "ix_notification_outbox_alert_created",
            "alert_id",
            "created_at",
        ),
        Index("ix_notification_outbox_created_at", "created_at"),
        Index(
            "ix_notification_outbox_status_updated",
            "status",
            "updated_at",
        ),
        Index(
            "ix_notification_outbox_alert_status",
            "alert_id",
            "status",
        ),
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

    __table_args__ = (
        Index("ix_drones_last_seen", "last_seen"),
    )


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
        Index(
            "ix_observations_received_observation",
            "received_at",
            "observation_id",
        ),
        Index(
            "ix_observations_station_received",
            "station_id",
            "received_at",
        ),
        Index(
            "ix_observations_operator_received",
            "operator_id",
            "received_at",
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
