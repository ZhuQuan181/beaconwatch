"""Alert keyset, statistics and retention indexes.

Revision ID: 0008_alert_queries
Revises: 0007_notification_outbox
Create Date: 2026-11-16
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0008_alert_queries"
down_revision: Union[str, None] = "0007_notification_outbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_alerts_triggered_at"))
    op.create_index(
        "ix_alerts_triggered_alert",
        "alerts",
        ["triggered_at", "alert_id"],
    )
    op.create_index(
        "ix_alerts_status_resolved",
        "alerts",
        ["status", "resolved_at"],
    )
    op.create_index(
        "ix_alerts_rule_triggered",
        "alerts",
        ["rule", "triggered_at"],
    )
    op.create_index(
        "ix_alerts_severity_triggered",
        "alerts",
        ["severity", "triggered_at"],
    )
    op.create_index(
        "ix_notification_outbox_status_updated",
        "notification_outbox",
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_notification_outbox_alert_status",
        "notification_outbox",
        ["alert_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_notification_outbox_alert_status",
        table_name="notification_outbox",
    )
    op.drop_index(
        "ix_notification_outbox_status_updated",
        table_name="notification_outbox",
    )
    op.drop_index(
        "ix_alerts_severity_triggered",
        table_name="alerts",
    )
    op.drop_index("ix_alerts_rule_triggered", table_name="alerts")
    op.drop_index("ix_alerts_status_resolved", table_name="alerts")
    op.drop_index("ix_alerts_triggered_alert", table_name="alerts")
    op.create_index(
        "ix_alerts_triggered_at",
        "alerts",
        ["triggered_at"],
    )
