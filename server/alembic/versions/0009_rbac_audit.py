"""Named API identities, request ids and resource audit.

Revision ID: 0009_rbac_audit
Revises: 0008_alert_queries
Create Date: 2026-11-16
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009_rbac_audit"
down_revision: Union[str, None] = "0008_alert_queries"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_events",
        sa.Column("actor_subject", sa.String(128), nullable=True),
    )
    op.add_column(
        "alert_events",
        sa.Column("request_id", sa.String(128), nullable=True),
    )
    op.create_index(
        "ix_alert_events_actor_subject",
        "alert_events",
        ["actor_subject"],
    )

    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(80), primary_key=True),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(128), nullable=True),
        sa.Column("actor_subject", sa.String(128), nullable=True),
        sa.Column("actor_name", sa.String(128), nullable=True),
        sa.Column("actor_roles", sa.JSON(), nullable=False),
        sa.Column(
            "authenticated",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_id", sa.String(128), nullable=False),
        sa.Column(
            "outcome",
            sa.String(16),
            nullable=False,
            server_default="success",
        ),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column(
            "status_code",
            sa.Integer(),
            nullable=False,
            server_default="200",
        ),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('success', 'noop')",
            name="ck_audit_events_outcome",
        ),
    )
    op.create_index(
        "ix_audit_events_occurred_event",
        "audit_events",
        ["occurred_at", "event_id"],
    )
    op.create_index(
        "ix_audit_events_actor_occurred",
        "audit_events",
        ["actor_subject", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_resource_occurred",
        "audit_events",
        ["resource_type", "resource_id", "occurred_at"],
    )
    op.create_index(
        "ix_audit_events_action_occurred",
        "audit_events",
        ["action", "occurred_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_audit_events_action_occurred",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_resource_occurred",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_actor_occurred",
        table_name="audit_events",
    )
    op.drop_index(
        "ix_audit_events_occurred_event",
        table_name="audit_events",
    )
    op.drop_table("audit_events")
    op.drop_index(
        "ix_alert_events_actor_subject",
        table_name="alert_events",
    )
    op.drop_column("alert_events", "request_id")
    op.drop_column("alert_events", "actor_subject")
