"""Indexes for recent-drone and cursor-pagination queries.

The unused ``update`` compensation path in observation ingest has been
removed as well; this migration only changes indexes.

Revision ID: 0004_query_indexes
Revises: 0003_coverage_geofence
Create Date: 2026-11-15
"""
from __future__ import annotations

from typing import Union

from alembic import op

revision: str = "0004_query_indexes"
down_revision: Union[str, None] = "0003_coverage_geofence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_drones_last_seen", "drones", ["last_seen"])
    op.create_index(
        "ix_observations_received_observation",
        "observations",
        ["received_at", "observation_id"],
    )
    op.create_index(
        "ix_observations_station_received",
        "observations",
        ["station_id", "received_at"],
    )
    op.create_index(
        "ix_observations_operator_received",
        "observations",
        ["operator_id", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_observations_operator_received",
        table_name="observations",
    )
    op.drop_index(
        "ix_observations_station_received",
        table_name="observations",
    )
    op.drop_index(
        "ix_observations_received_observation",
        table_name="observations",
    )
    op.drop_index("ix_drones_last_seen", table_name="drones")
