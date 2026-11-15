"""Persistent alert ledger and operator acknowledgements.

The alert rules remain derived from stored observations, but operators
need their alert history and first acknowledgement to survive a server
restart.  This table is the durable record behind ``GET /alerts`` and
``POST /alerts/{alert_id}/ack``.

Revision ID: 0005_alerts
Revises: 0004_query_indexes
Create Date: 2026-11-15
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005_alerts"
down_revision: Union[str, None] = "0004_query_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "alerts",
        sa.Column("alert_id", sa.String(64), primary_key=True),
        sa.Column("rule", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("drone_id", sa.String(64), nullable=False),
        sa.Column("message", sa.String(512), nullable=False),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "trigger_count",
            sa.BigInteger(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_by", sa.String(128)),
        sa.Column("acknowledgement_note", sa.Text()),
    )
    op.create_index("ix_alerts_triggered_at", "alerts", ["triggered_at"])
    op.create_index("ix_alerts_drone_id", "alerts", ["drone_id"])
    op.create_index(
        "ix_alerts_acknowledged_at", "alerts", ["acknowledged_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_alerts_acknowledged_at", table_name="alerts")
    op.drop_index("ix_alerts_drone_id", table_name="alerts")
    op.drop_index("ix_alerts_triggered_at", table_name="alerts")
    op.drop_table("alerts")
