"""Add observations.rssi_dbm.

The edge has always captured RSSI; the server schema simply had no
column for it, so every observation's signal strength was discarded on
ingest.  That made "which station hears this drone best" — the whole
point of a multi-station deployment — unanswerable after the fact.

Revision ID: 0002_rssi
Revises: 0001_initial
Create Date: 2026-11-14
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002_rssi"
down_revision: Union[str, None] = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "observations",
        sa.Column("rssi_dbm", sa.Integer(), nullable=True),
    )
    # Multi-station queries ask "best signal per drone in the window";
    # a plain btree on the column is enough to keep those from scanning.
    op.create_index(
        "ix_observations_rssi_dbm",
        "observations",
        ["rssi_dbm"],
    )


def downgrade() -> None:
    op.drop_index("ix_observations_rssi_dbm", table_name="observations")
    op.drop_column("observations", "rssi_dbm")
