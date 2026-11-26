"""Performance indexes for unidentified-drone and protocol queries.

The dashboard polls ``/drones/unidentified`` with a recent-window
predicate.  A partial index over rows with no operator ID lets both
PostgreSQL and SQLite satisfy that scan without walking the full
observation history.

Revision ID: 0010_perf_indexes
Revises: 0009_rbac_audit
Create Date: 2026-11-27
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0010_perf_indexes"
down_revision: Union[str, None] = "0009_rbac_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_observations_unidentified_drone_received",
        "observations",
        ["drone_id", "received_at"],
        postgresql_where=sa.text("operator_id IS NULL"),
    )
    op.create_index(
        "ix_observations_protocol_received",
        "observations",
        ["protocol", "received_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_observations_protocol_received",
        table_name="observations",
    )
    op.drop_index(
        "ix_observations_unidentified_drone_received",
        table_name="observations",
    )
