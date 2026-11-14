"""Initial schema: stations, drones, observations.

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-06

Notes
-----
* Uses **plain lat/lon Float columns** instead of PostGIS ``Geometry`` for
  portability — the alpine-based ``timescaledb`` image we ship does not
  include ``postgresql16-postgis``.  Bbox queries are still fast enough at
  our expected cardinality (≪ 1 M rows / day) with a composite index.
* TimescaleDB extension is still loaded so the observations table can be
  converted to a hypertable.  Continuous aggregate created in step 6.
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 1) TimescaleDB for the hypertable conversion below.  PostGIS is
    #    intentionally NOT enabled here — we run on stock PG with float
    #    lat/lon columns for portability.
    op.execute("CREATE EXTENSION IF NOT EXISTS timescaledb;")

    # 2) Stations (static reference)
    op.create_table(
        "stations",
        sa.Column("station_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("lat", sa.Float, nullable=False),
        sa.Column("lon", sa.Float, nullable=False),
        sa.Column("alt_m", sa.Float, nullable=False, server_default="0"),
        sa.Column("online", sa.Boolean, server_default=sa.text("true")),
        sa.Column("last_seen", sa.DateTime(timezone=True)),
        sa.Column("firmware_version", sa.String(32), server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
        ),
    )

    # 3) Drones (one row per UAS ID)
    op.create_table(
        "drones",
        sa.Column("drone_id", sa.String(64), primary_key=True),
        sa.Column("uas_id", sa.String(64), nullable=False),
        sa.Column("ua_type", sa.String(32), server_default="unknown"),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_position_lat", sa.Float, nullable=False),
        sa.Column("last_position_lon", sa.Float, nullable=False),
        sa.Column("last_position_alt_m", sa.Float, nullable=False),
        sa.Column("last_station_id", sa.String(64), nullable=False),
        sa.Column("observation_count", sa.BigInteger, server_default="0"),
    )

    # 4) Observations (time-series, will become a hypertable)
    #
    #    The primary key is composite ``(observation_id, received_at)``:
    #    TimescaleDB requires the partitioning column to participate in
    #    every unique index it builds on the hypertable, otherwise
    #    ``create_hypertable`` fails with "cannot create a unique
    #    index without the column received_at".
    op.create_table(
        "observations",
        sa.Column("observation_id", sa.String(64), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "drone_id",
            sa.String(64),
            sa.ForeignKey("drones.drone_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "station_id",
            sa.String(64),
            sa.ForeignKey("stations.station_id", ondelete="SET NULL"),
            nullable=False,
        ),
        sa.Column("protocol", sa.String(32), nullable=False),
        sa.Column("drone_timestamp", sa.DateTime(timezone=True)),
        sa.Column("position_lat", sa.Float, nullable=False),
        sa.Column("position_lon", sa.Float, nullable=False),
        sa.Column("position_alt_m", sa.Float, nullable=False),
        sa.Column("speed_mps", sa.Float, server_default="0"),
        sa.Column("heading_deg", sa.Float, server_default="0"),
        sa.Column("self_id", sa.String(256), server_default=""),
        sa.Column("operator_id", sa.String(64)),
        sa.Column("raw", sa.JSON, server_default="{}"),
        sa.PrimaryKeyConstraint("observation_id", "received_at"),
    )
    op.create_index(
        "ix_observations_drone_id", "observations", ["drone_id"]
    )
    op.create_index(
        "ix_observations_station_id", "observations", ["station_id"]
    )
    op.create_index(
        "ix_observations_received_at", "observations", ["received_at"]
    )
    op.create_index(
        "ix_observations_drone_received",
        "observations",
        ["drone_id", "received_at"],
    )
    # Bbox queries (lat BETWEEN … AND lon BETWEEN …)
    op.create_index(
        "ix_observations_lat_lon", "observations", ["position_lat", "position_lon"]
    )

    # 5) Convert observations to a TimescaleDB hypertable partitioned
    #    by received_at (1-day chunks).
    op.execute(
        "SELECT create_hypertable("
        "'observations', 'received_at', "
        "chunk_time_interval => INTERVAL '1 day', "
        "if_not_exists => TRUE);"
    )

    # 6) Convenience: a 5-minute continuous aggregate of drone count
    #    per station.
    op.execute(
        """
        CREATE MATERIALIZED VIEW IF NOT EXISTS obs_per_station_5m
        WITH (timescaledb.continuous) AS
        SELECT
            station_id,
            time_bucket(INTERVAL '5 minutes', received_at) AS bucket,
            count(*)                                       AS n
        FROM observations
        GROUP BY station_id, bucket
        WITH NO DATA;
        """
    )
    op.execute(
        """
        SELECT add_continuous_aggregate_policy(
            'obs_per_station_5m',
            start_offset => INTERVAL '1 hour',
            end_offset   => INTERVAL '5 minutes',
            schedule_interval => INTERVAL '5 minutes'
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS obs_per_station_5m;")
    op.execute(
        "SELECT remove_hypertable('observations', if_exists => TRUE);"
    )
    op.drop_table("observations")
    op.drop_table("drones")
    op.drop_table("stations")
