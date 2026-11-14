"""Station coverage radius + geofence config.

Two things were missing to make "alerts" meaningful:

1. ``stations.coverage_radius_m`` — a receiver's reach.  Without it the
   map can't show what each station is responsible for, and an operator
   can't see the gaps between sites.

2. ``geofence_zones`` — the no-fly polygons that alerts fire against.
   Kept in the DB rather than a config file so the map and the rule
   engine read the *same* geometry; two sources would drift and the
   red box on screen would not match what triggered the alert.

Both columns are nullable/defaulted so existing rows stay valid.

Revision ID: 0003_coverage_geofence
Revises: 0002_rssi
Create Date: 2026-11-14
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003_coverage_geofence"
down_revision: Union[str, None] = "0002_rssi"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "stations",
        sa.Column(
            "coverage_radius_m",
            sa.Float(),
            nullable=False,
            server_default="3000",
        ),
    )

    op.create_table(
        "geofence_zones",
        sa.Column("zone_id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        # Polygon as an ordered list of [lon, lat] pairs (GeoJSON ring
        # order, no closing point).  JSON keeps the API and the rule
        # engine reading identical geometry without requiring PostGIS,
        # which the alpine timescaledb image doesn't ship.
        sa.Column("polygon", sa.JSON(), nullable=False),
        sa.Column("min_alt_m", sa.Float(), nullable=True),
        sa.Column("max_alt_m", sa.Float(), nullable=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="warn"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("geofence_zones")
    op.drop_column("stations", "coverage_radius_m")
