"""Alert pagination, aggregate statistics and retention tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
from rid_server.alerts import Alert
from rid_server.app import create_app
from rid_server.bus import ObservationBus
from rid_server.config import Settings
from rid_server.db import AlertEvent, AlertRecord, NotificationOutbox
from rid_server.mqtt_subscriber import MqttSubscriber
from rid_server.pagination import encode_cursor
from rid_server.repository import (
    acknowledge_alert,
    get_alert_stats,
    purge_expired_notifications,
    purge_expired_resolved_alerts,
    resolve_alert,
    upsert_alert,
)
from rid_server.retention import RetentionWorker
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker


def make_alert(
    *,
    alert_id: str,
    at: datetime,
    rule: str = "altitude",
    severity: str = "warn",
    drone_id: str = "drone-A",
) -> Alert:
    return Alert(
        alert_id=alert_id,
        rule=rule,
        severity=severity,
        drone_id=drone_id,
        message=f"alert {alert_id}",
        detail={"source": "test"},
        at=at.isoformat(),
    )


def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


def _app():
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(Settings())
    return app


async def _factory(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


async def test_alert_cursor_paginates_tied_timestamps_without_duplicates(
    db_engine,
):
    base = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
    factory = await _factory(db_engine)
    async with factory() as session:
        await upsert_alert(session, make_alert(alert_id="tie-a", at=base))
        await upsert_alert(session, make_alert(alert_id="tie-b", at=base))
        await upsert_alert(session, make_alert(alert_id="tie-c", at=base))
        await upsert_alert(
            session,
            make_alert(alert_id="newest", at=base + timedelta(seconds=1)),
        )
        await upsert_alert(
            session,
            make_alert(alert_id="oldest", at=base - timedelta(seconds=1)),
        )

    app = _app()
    async with _client(app) as client:
        first = await client.get("/alerts/page", params={"limit": 2})
        assert first.status_code == 200
        first_body = first.json()
        assert [row["alert_id"] for row in first_body["items"]] == [
            "newest",
            "tie-c",
        ]
        assert first_body["next_cursor"]

        second = await client.get(
            "/alerts/page",
            params={"limit": 2, "cursor": first_body["next_cursor"]},
        )
        assert second.status_code == 200
        second_body = second.json()
        assert [row["alert_id"] for row in second_body["items"]] == [
            "tie-b",
            "tie-a",
        ]
        assert second_body["next_cursor"]

        third = await client.get(
            "/alerts/page",
            params={"limit": 2, "cursor": second_body["next_cursor"]},
        )

        legacy = await client.get("/alerts", params={"limit": 2})

    assert [
        row["alert_id"] for row in third.json()["items"]
    ] == ["oldest"]
    assert third.json()["next_cursor"] is None
    assert [row["alert_id"] for row in legacy.json()] == [
        "newest",
        "tie-c",
    ]
    assert legacy.headers["x-next-cursor"]


async def test_alert_cursor_rejects_malformed_and_observation_cursors(
    db_engine,
):
    app = _app()
    observation_cursor = encode_cursor(
        datetime(2026, 11, 16, tzinfo=timezone.utc),
        "obs-1",
    )
    async with _client(app) as client:
        malformed = await client.get(
            "/alerts/page", params={"cursor": "not-a-cursor"}
        )
        wrong_type = await client.get(
            "/alerts/page", params={"cursor": observation_cursor}
        )

    assert malformed.status_code == 400
    assert wrong_type.status_code == 400


async def test_alert_page_respects_filters_with_cursor(db_engine):
    base = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
    factory = await _factory(db_engine)
    async with factory() as session:
        await upsert_alert(
            session,
            make_alert(
                alert_id="geo-1",
                at=base,
                rule="geofence",
                drone_id="drone-A",
            ),
        )
        await upsert_alert(
            session,
            make_alert(
                alert_id="geo-2",
                at=base + timedelta(seconds=1),
                rule="geofence",
                drone_id="drone-A",
            ),
        )
        await upsert_alert(
            session,
            make_alert(
                alert_id="geo-other",
                at=base + timedelta(seconds=2),
                rule="geofence",
                drone_id="drone-B",
            ),
        )

    app = _app()
    async with _client(app) as client:
        first = await client.get(
            "/alerts/page",
            params={"limit": 1, "rule": "geofence", "drone_id": "drone-A"},
        )
        cursor = first.json()["next_cursor"]
        second = await client.get(
            "/alerts/page",
            params={
                "limit": 1,
                "rule": "geofence",
                "drone_id": "drone-A",
                "cursor": cursor,
            },
        )

    assert [row["alert_id"] for row in first.json()["items"]] == ["geo-2"]
    assert [row["alert_id"] for row in second.json()["items"]] == ["geo-1"]
    assert second.json()["next_cursor"] is None


async def test_alert_stats_counts_and_filters(session):
    base = datetime(2026, 11, 16, 8, 0, tzinfo=timezone.utc)
    await upsert_alert(
        session,
        make_alert(
            alert_id="open-geo",
            at=base,
            rule="geofence",
            severity="critical",
        ),
    )
    await upsert_alert(
        session,
        make_alert(
            alert_id="acked-ghost",
            at=base + timedelta(seconds=1),
            rule="unidentified",
            severity="warn",
            drone_id="drone-B",
        ),
    )
    await upsert_alert(
        session,
        make_alert(
            alert_id="resolved-alt",
            at=base + timedelta(seconds=2),
            rule="altitude",
            severity="info",
        ),
    )
    await acknowledge_alert(
        session,
        "acked-ghost",
        acknowledged_by="operator-1",
    )
    await resolve_alert(
        session,
        "resolved-alt",
        resolved_by="operator-2",
    )

    stats = await get_alert_stats(session)
    active = await get_alert_stats(session, status="active")
    altitude = await get_alert_stats(session, rule="altitude")

    assert stats["total"] == 3
    assert stats["open"] == 1
    assert stats["acknowledged"] == 1
    assert stats["resolved"] == 1
    assert stats["active"] == 2
    assert stats["by_severity"] == {
        "critical": 1,
        "warn": 1,
        "info": 1,
    }
    assert stats["by_rule"] == {
        "geofence": 1,
        "unidentified": 1,
        "altitude": 1,
    }
    assert stats["oldest_active_at"]
    assert active["total"] == 2
    assert active["open"] == 1
    assert active["acknowledged"] == 1
    assert active["resolved"] == 0
    assert altitude["total"] == 1
    assert altitude["resolved"] == 1
    assert altitude["oldest_active_at"] is None


async def test_alert_stats_route_is_not_shadowed_by_alert_id_route(db_engine):
    app = _app()
    async with _client(app) as client:
        response = await client.get("/alerts/stats")

    assert response.status_code == 200
    assert response.json()["total"] == 0
    assert response.json()["by_severity"] == {}


async def test_retention_deletes_only_old_resolved_alerts_with_terminal_notifications(
    db_engine,
):
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=40)
    factory = await _factory(db_engine)
    async with factory() as session:
        await upsert_alert(
            session, make_alert(alert_id="old-resolved", at=old)
        )
        await resolve_alert(
            session,
            "old-resolved",
            resolved_by="operator",
            now=old + timedelta(minutes=1),
        )
        await upsert_alert(session, make_alert(alert_id="old-open", at=old))
        await upsert_alert(
            session,
            make_alert(alert_id="recent-resolved", at=now),
        )
        await resolve_alert(
            session,
            "recent-resolved",
            resolved_by="operator",
            now=now,
        )
        await upsert_alert(
            session, make_alert(alert_id="protected-resolved", at=old)
        )
        await resolve_alert(
            session,
            "protected-resolved",
            resolved_by="operator",
            now=old + timedelta(minutes=1),
            enqueue_notification=True,
        )

    async with factory() as session:
        deleted = await purge_expired_resolved_alerts(
            session,
            older_than=now - timedelta(days=30),
            batch_size=10,
        )

    async with factory() as session:
        remaining = set(
            (await session.execute(select(AlertRecord.alert_id))).scalars()
        )
        old_events = (
            await session.execute(
                select(AlertEvent).where(
                    AlertEvent.alert_id == "old-resolved"
                )
            )
        ).scalars().all()
        pending = (
            await session.execute(
                select(NotificationOutbox).where(
                    NotificationOutbox.alert_id == "protected-resolved"
                )
            )
        ).scalars().all()

    assert deleted == 1
    assert remaining == {"old-open", "recent-resolved", "protected-resolved"}
    assert old_events == []
    assert len(pending) == 1
    assert pending[0].status == "pending"


async def test_notification_retention_removes_only_terminal_rows(
    db_engine,
):
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=40)
    factory = await _factory(db_engine)
    async with factory() as session:
        await upsert_alert(
            session, make_alert(alert_id="outbox-owner", at=old)
        )
        for index, status in enumerate(
            ("delivered", "dead", "pending", "delivering")
        ):
            session.add(
                NotificationOutbox(
                    delivery_id=f"delivery-{status}",
                    alert_id="outbox-owner",
                    kind="alert.triggered",
                    payload={},
                    status=status,
                    attempts=1,
                    failures=0,
                    next_attempt_at=old,
                    created_at=old,
                    updated_at=old,
                    delivered_at=old if status == "delivered" else None,
                )
            )
        session.add(
            NotificationOutbox(
                delivery_id="delivery-new",
                alert_id="outbox-owner",
                kind="alert.triggered",
                payload={},
                status="delivered",
                attempts=1,
                failures=0,
                next_attempt_at=now,
                created_at=now,
                updated_at=now,
                delivered_at=now,
            )
        )
        await session.commit()

    async with factory() as session:
        deleted = await purge_expired_notifications(
            session,
            older_than=now - timedelta(days=30),
            batch_size=10,
        )

    async with factory() as session:
        remaining = set(
            (
                await session.execute(
                    select(NotificationOutbox.delivery_id)
                )
            ).scalars()
        )

    assert deleted == 2
    assert remaining == {
        "delivery-pending",
        "delivery-delivering",
        "delivery-new",
    }


async def test_retention_worker_batches_and_records_counters(db_engine):
    now = datetime.now(tz=timezone.utc)
    old = now - timedelta(days=40)
    factory = await _factory(db_engine)
    async with factory() as session:
        for index in range(3):
            alert_id = f"retention-{index}"
            await upsert_alert(
                session, make_alert(alert_id=alert_id, at=old)
            )
            await resolve_alert(
                session,
                alert_id,
                resolved_by="operator",
                now=old + timedelta(minutes=1),
            )

    settings = Settings(
        alert_retention_days=30,
        retention_batch_size=2,
        retention_interval_s=60,
    )
    worker = RetentionWorker(settings, session_factory=factory)
    await worker.run_once()

    async with factory() as session:
        remaining = await session.execute(
            select(AlertRecord.alert_id).where(
                AlertRecord.alert_id.like("retention-%")
            )
        )

    assert remaining.scalars().all() == []
    assert worker.alerts_deleted == 3
    assert worker.cycles == 1
    assert worker.last_success_at is not None


async def test_metrics_expose_retention_counters(db_engine):
    app = _app()
    async with _client(app) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "rid_retention_worker_running 0" in response.text
    assert "rid_retention_alerts_deleted_total 0" in response.text
    assert "rid_retention_notifications_deleted_total 0" in response.text
    assert "rid_retention_failures_total 0" in response.text
