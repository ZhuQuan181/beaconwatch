"""Alert lifecycle, operator workflow audit trail and filter indexes.

Revision ID: 0006_alert_lifecycle
Revises: 0005_alerts
Create Date: 2026-11-15
"""
from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006_alert_lifecycle"
down_revision: Union[str, None] = "0005_alerts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alerts",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.add_column(
        "alerts",
        sa.Column(
            "status",
            sa.String(16),
            server_default="open",
            nullable=False,
        ),
    )
    op.add_column(
        "alerts",
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "alerts",
        sa.Column("resolved_by", sa.String(128)),
    )
    op.add_column(
        "alerts",
        sa.Column("resolution_note", sa.Text()),
    )
    op.add_column(
        "alerts",
        sa.Column(
            "reopen_count",
            sa.Integer(),
            server_default="0",
            nullable=False,
        ),
    )

    op.execute(
        sa.text(
            "UPDATE alerts SET status = CASE "
            "WHEN acknowledged_at IS NULL THEN 'open' "
            "ELSE 'acknowledged' END"
        )
    )
    op.create_check_constraint(
        "ck_alerts_status",
        "alerts",
        "status IN ('open', 'acknowledged', 'resolved')",
    )
    op.create_index(
        "ix_alerts_status_triggered",
        "alerts",
        ["status", "triggered_at"],
    )
    op.create_index(
        "ix_alerts_drone_triggered",
        "alerts",
        ["drone_id", "triggered_at"],
    )

    op.create_table(
        "alert_events",
        sa.Column("event_id", sa.String(80), primary_key=True),
        sa.Column(
            "alert_id",
            sa.String(64),
            sa.ForeignKey("alerts.alert_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("action", sa.String(24), nullable=False),
        sa.Column("actor", sa.String(128)),
        sa.Column("note", sa.Text()),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("detail", sa.JSON(), nullable=False),
    )
    op.create_index(
        "ix_alert_events_alert_id",
        "alert_events",
        ["alert_id"],
    )
    op.create_index(
        "ix_alert_events_alert_occurred",
        "alert_events",
        ["alert_id", "occurred_at"],
    )
    op.execute(
        sa.text(
            "INSERT INTO alert_events "
            "(event_id, alert_id, action, actor, note, occurred_at, detail) "
            "SELECT alert_id || '-triggered', alert_id, 'triggered', "
            "'system', NULL, first_seen_at, "
            "json_build_object('rule', rule, 'severity', severity, "
            "'drone_id', drone_id, 'backfilled', true) "
            "FROM alerts"
        )
    )
    op.execute(
        sa.text(
            "INSERT INTO alert_events "
            "(event_id, alert_id, action, actor, note, occurred_at, detail) "
            "SELECT alert_id || '-acknowledged', alert_id, 'acknowledged', "
            "acknowledged_by, acknowledgement_note, acknowledged_at, "
            "json_build_object('backfilled', true) "
            "FROM alerts WHERE acknowledged_at IS NOT NULL"
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_alert_events_alert_occurred",
        table_name="alert_events",
    )
    op.drop_index("ix_alert_events_alert_id", table_name="alert_events")
    op.drop_table("alert_events")
    op.drop_index("ix_alerts_drone_triggered", table_name="alerts")
    op.drop_index("ix_alerts_status_triggered", table_name="alerts")
    op.drop_constraint("ck_alerts_status", "alerts", type_="check")
    op.drop_column("alerts", "reopen_count")
    op.drop_column("alerts", "resolution_note")
    op.drop_column("alerts", "resolved_by")
    op.drop_column("alerts", "resolved_at")
    op.drop_column("alerts", "status")
    op.drop_column("alerts", "updated_at")
