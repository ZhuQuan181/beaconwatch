"""Durable webhook notification outbox.

Alert lifecycle transitions and their webhook delivery intent are
committed atomically.  The worker claims due rows with a lease so a
restarted process can recover an interrupted attempt.

Revision ID: 0007_notification_outbox
Revises: 0006_alert_lifecycle
Create Date: 2026-11-16
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_notification_outbox"
down_revision: Union[str, None] = "0006_alert_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notification_outbox",
        sa.Column("delivery_id", sa.String(64), primary_key=True),
        sa.Column(
            "alert_id",
            sa.String(64),
            sa.ForeignKey("alerts.alert_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(16),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "failures",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("lease_id", sa.String(64)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'delivering', 'delivered', 'dead')",
            name="ck_notification_outbox_status",
        ),
    )
    op.create_index(
        "ix_notification_outbox_status_next_attempt",
        "notification_outbox",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_notification_outbox_alert_created",
        "notification_outbox",
        ["alert_id", "created_at"],
    )
    op.create_index(
        "ix_notification_outbox_created_at",
        "notification_outbox",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_outbox_created_at",
        table_name="notification_outbox",
    )
    op.drop_index(
        "ix_notification_outbox_alert_created",
        table_name="notification_outbox",
    )
    op.drop_index(
        "ix_notification_outbox_status_next_attempt",
        table_name="notification_outbox",
    )
    op.drop_table("notification_outbox")
