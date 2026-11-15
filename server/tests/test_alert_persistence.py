"""Durable alert ledger, acknowledgement and real-time fallback tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from rid_server import repository
from rid_server.alerts import Alert, AlertRing
from rid_server.app import create_app
from rid_server.bus import ObservationBus
from rid_server.config import Settings
from rid_server.mqtt_subscriber import MqttSubscriber
from rid_server.repository import (
    AlertStateConflict,
    acknowledge_alert,
    list_alert_events,
    list_alerts,
    reopen_alert,
    resolve_alert,
    upsert_alert,
)
from rid_server.settings_state import set_subscriber_bus_hook
from sqlalchemy.ext.asyncio import async_sessionmaker


def make_alert(
    *,
    alert_id: str = "altitude-abc123",
    at: datetime | None = None,
    message: str = "测试告警",
    rule: str = "altitude",
    drone_id: str = "drone-A",
) -> Alert:
    observed_at = at or datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    return Alert(
        alert_id=alert_id,
        rule=rule,
        severity="warn",
        drone_id=drone_id,
        message=message,
        detail={"alt_m": 180.0, "limit_m": 120.0},
        at=observed_at.isoformat(),
    )


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


# ── Repository ────────────────────────────────────────────────────────
async def test_upsert_alert_updates_recurrence_without_duplicate(session):
    first_at = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    later_at = first_at + timedelta(minutes=5)

    inserted = await upsert_alert(session, make_alert(at=first_at))
    updated = await upsert_alert(
        session,
        make_alert(at=later_at, message="更新后的告警"),
    )

    assert inserted.alert_id == updated.alert_id
    assert updated.trigger_count == 2
    assert as_utc(updated.first_seen_at) == first_at
    assert as_utc(updated.triggered_at) == later_at
    assert updated.message == "更新后的告警"
    assert len(await list_alerts(session)) == 1


async def test_upsert_alert_does_not_move_trigger_time_backwards(session):
    first_at = datetime(2026, 11, 15, 8, 5, tzinfo=timezone.utc)
    older_at = first_at - timedelta(minutes=1)

    await upsert_alert(session, make_alert(at=first_at))
    row = await upsert_alert(session, make_alert(at=older_at))

    assert row.trigger_count == 2
    assert as_utc(row.first_seen_at) == older_at
    assert as_utc(row.triggered_at) == first_at


async def test_acknowledge_alert_is_idempotent(session):
    first_at = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    ack_at = first_at + timedelta(minutes=1)
    retry_at = first_at + timedelta(minutes=2)
    await upsert_alert(session, make_alert(at=first_at))

    first = await acknowledge_alert(
        session,
        "altitude-abc123",
        acknowledged_by="alice",
        note="已派发现场核查",
        now=ack_at,
    )
    retry = await acknowledge_alert(
        session,
        "altitude-abc123",
        acknowledged_by="bob",
        note="retry must not overwrite",
        now=retry_at,
    )

    assert first is not None
    assert retry is not None
    assert retry.acknowledged_by == "alice"
    assert retry.acknowledgement_note == "已派发现场核查"
    assert as_utc(retry.acknowledged_at) == ack_at


async def test_acknowledge_missing_alert_returns_none(session):
    assert (
        await acknowledge_alert(
            session, "missing", acknowledged_by="alice"
        )
        is None
    )


async def test_alert_lifecycle_is_persisted_and_audited(session):
    first_at = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    ack_at = first_at + timedelta(minutes=1)
    resolve_at = first_at + timedelta(minutes=2)
    reopen_at = first_at + timedelta(minutes=3)
    created = await upsert_alert(session, make_alert(at=first_at))

    assert created.status == "open"
    assert created.reopen_count == 0

    acknowledged = await acknowledge_alert(
        session,
        created.alert_id,
        acknowledged_by="alice",
        note="已派发",
        now=ack_at,
    )
    assert acknowledged is not None
    assert acknowledged.status == "acknowledged"

    resolved = await resolve_alert(
        session,
        created.alert_id,
        resolved_by="bob",
        note="现场核查完成",
        now=resolve_at,
    )
    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.resolved_by == "bob"
    assert resolved.resolution_note == "现场核查完成"

    reopened = await reopen_alert(
        session,
        created.alert_id,
        reopened_by="carol",
        note="发现新的航迹",
        now=reopen_at,
    )

    assert reopened is not None
    assert reopened.status == "open"
    assert reopened.reopen_count == 1
    assert reopened.acknowledged_at is None
    assert reopened.resolved_at is None

    events = await list_alert_events(session, created.alert_id)
    assert [event.action for event in events] == [
        "triggered",
        "acknowledged",
        "resolved",
        "reopened",
    ]
    assert [event.actor for event in events] == [
        "system",
        "alice",
        "bob",
        "carol",
    ]
    assert events[-1].detail["previous_resolution_note"] == "现场核查完成"


async def test_resolve_is_idempotent_but_acknowledging_resolved_conflicts(
    session,
):
    first_at = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    await upsert_alert(session, make_alert(at=first_at))

    first = await resolve_alert(
        session,
        "altitude-abc123",
        resolved_by="alice",
        note="closed",
        now=first_at + timedelta(minutes=1),
    )
    retry = await resolve_alert(
        session,
        "altitude-abc123",
        resolved_by="bob",
        note="retry",
        now=first_at + timedelta(minutes=2),
    )

    assert first is not None
    assert retry is not None
    assert retry.resolved_by == "alice"
    assert retry.resolution_note == "closed"
    with pytest.raises(AlertStateConflict):
        await acknowledge_alert(
            session,
            "altitude-abc123",
            acknowledged_by="bob",
        )
    events = await list_alert_events(session, "altitude-abc123")
    assert [event.action for event in events] == ["triggered", "resolved"]


async def test_only_resolved_alert_can_be_reopened(session):
    first_at = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    await upsert_alert(session, make_alert(at=first_at))

    with pytest.raises(AlertStateConflict):
        await reopen_alert(
            session,
            "altitude-abc123",
            reopened_by="alice",
        )


async def test_recurrence_after_resolution_auto_reopens(session):
    first_at = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    await upsert_alert(session, make_alert(at=first_at))
    await acknowledge_alert(
        session,
        "altitude-abc123",
        acknowledged_by="alice",
        now=first_at + timedelta(minutes=1),
    )
    await resolve_alert(
        session,
        "altitude-abc123",
        resolved_by="bob",
        note="closed",
        now=first_at + timedelta(minutes=2),
    )

    reopened = await upsert_alert(
        session,
        make_alert(at=first_at + timedelta(minutes=5)),
    )

    assert reopened.status == "open"
    assert reopened.trigger_count == 2
    assert reopened.reopen_count == 1
    assert reopened.acknowledged_at is None
    assert reopened.acknowledged_by is None
    assert reopened.resolved_at is None
    assert reopened.resolved_by is None
    events = await list_alert_events(session, "altitude-abc123")
    assert [event.action for event in events] == [
        "triggered",
        "acknowledged",
        "resolved",
        "reopened",
    ]
    assert events[-1].actor == "system"
    assert events[-1].detail["automatic"] is True


async def test_list_alerts_filters_lifecycle_rule_drone_and_window(session):
    base = datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    await upsert_alert(
        session,
        make_alert(alert_id="open-a", at=base, drone_id="drone-A"),
    )
    await upsert_alert(
        session,
        make_alert(
            alert_id="acked-b",
            at=base + timedelta(minutes=1),
            rule="unidentified",
            drone_id="drone-B",
        ),
    )
    await upsert_alert(
        session,
        make_alert(
            alert_id="resolved-c",
            at=base + timedelta(minutes=2),
            drone_id="drone-A",
        ),
    )
    await acknowledge_alert(
        session,
        "acked-b",
        acknowledged_by="alice",
    )
    await resolve_alert(
        session,
        "resolved-c",
        resolved_by="bob",
    )

    assert [row.alert_id for row in await list_alerts(session, status="open")] == [
        "open-a"
    ]
    assert [
        row.alert_id
        for row in await list_alerts(session, status="acknowledged")
    ] == ["acked-b"]
    assert [
        row.alert_id for row in await list_alerts(session, status="resolved")
    ] == ["resolved-c"]
    assert {
        row.alert_id for row in await list_alerts(session, status="active")
    } == {"open-a", "acked-b"}
    assert [
        row.alert_id
        for row in await list_alerts(session, drone_id="drone-A")
    ] == ["resolved-c", "open-a"]
    assert [
        row.alert_id
        for row in await list_alerts(session, rule="unidentified")
    ] == ["acked-b"]
    assert [
        row.alert_id
        for row in await list_alerts(
            session,
            since=base + timedelta(minutes=1),
            until=base + timedelta(minutes=1, seconds=30),
        )
    ] == ["acked-b"]


# ── HTTP API ──────────────────────────────────────────────────────────
def _client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


async def _seed_alert(db_engine, alert: Alert | None = None):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        return await upsert_alert(session, alert or make_alert())


async def test_alerts_api_lists_and_acknowledges(db_engine):
    row = await _seed_alert(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(Settings())

    async with _client(app) as client:
        listed = await client.get("/alerts")
        acknowledged = await client.post(
            f"/alerts/{row.alert_id}/ack",
            json={
                "acknowledged_by": "operator-7",
                "note": "现场人员已响应",
            },
        )
        retried = await client.post(
            f"/alerts/{row.alert_id}/ack",
            json={"acknowledged_by": "someone-else"},
        )

    assert listed.status_code == 200
    assert listed.json()[0]["alert_id"] == row.alert_id
    assert listed.json()[0]["acknowledged_at"] is None

    assert acknowledged.status_code == 200
    body = acknowledged.json()
    assert body["acknowledged_by"] == "operator-7"
    assert body["acknowledgement_note"] == "现场人员已响应"
    assert body["acknowledged_at"]
    assert body["status"] == "acknowledged"

    assert retried.status_code == 200
    assert retried.json()["acknowledged_by"] == "operator-7"


async def test_alerts_api_unknown_ack_is_404_and_empty_actor_is_422(db_engine):
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(Settings())

    async with _client(app) as client:
        missing = await client.post(
            "/alerts/does-not-exist/ack",
            json={"acknowledged_by": "operator-7"},
        )
        invalid = await client.post(
            "/alerts/does-not-exist/ack",
            json={"acknowledged_by": ""},
        )
        whitespace = await client.post(
            "/alerts/does-not-exist/ack",
            json={"acknowledged_by": "   "},
        )

    assert missing.status_code == 404
    assert invalid.status_code == 422
    assert whitespace.status_code == 422


async def test_alert_ack_uses_api_token_db_auth(db_engine):
    row = await _seed_alert(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(Settings())
    app.state.settings = Settings(api_token="alert-secret")

    async with _client(app) as client:
        denied = await client.post(
            f"/alerts/{row.alert_id}/ack",
            json={"acknowledged_by": "operator-7"},
        )
        allowed = await client.post(
            f"/alerts/{row.alert_id}/ack",
            json={"acknowledged_by": "operator-7"},
            headers={"Authorization": "Bearer alert-secret"},
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200


async def test_alert_lifecycle_api_and_event_history(db_engine):
    row = await _seed_alert(db_engine)
    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(Settings())

    async with _client(app) as client:
        resolved = await client.post(
            f"/alerts/{row.alert_id}/resolve",
            json={"resolved_by": "operator-7", "note": "处置完成"},
        )
        ack_after_resolve = await client.post(
            f"/alerts/{row.alert_id}/ack",
            json={"acknowledged_by": "operator-8"},
        )
        reopened = await client.post(
            f"/alerts/{row.alert_id}/reopen",
            json={"reopened_by": "operator-9", "note": "复飞"},
        )
        duplicate_reopen = await client.post(
            f"/alerts/{row.alert_id}/reopen",
            json={"reopened_by": "operator-9"},
        )
        events = await client.get(f"/alerts/{row.alert_id}/events")
        listed = await client.get("/alerts", params={"status": "open"})
        missing_events = await client.get("/alerts/missing/events")

    assert resolved.status_code == 200
    assert resolved.json()["status"] == "resolved"
    assert resolved.json()["resolved_by"] == "operator-7"
    assert resolved.json()["resolution_note"] == "处置完成"
    assert ack_after_resolve.status_code == 409
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "open"
    assert reopened.json()["reopen_count"] == 1
    assert duplicate_reopen.status_code == 409
    assert events.status_code == 200
    assert [item["action"] for item in events.json()] == [
        "triggered",
        "resolved",
        "reopened",
    ]
    assert listed.status_code == 200
    assert [item["alert_id"] for item in listed.json()] == [row.alert_id]
    assert missing_events.status_code == 404


async def test_resolve_broadcasts_and_filters_are_visible(db_engine):
    row = await _seed_alert(db_engine)
    app = create_app()
    bus = ObservationBus()
    app.state.bus = bus
    app.state.subscriber = MqttSubscriber(Settings())

    async with _client(app) as client:
        async with bus.subscribe() as queue:
            response = await client.post(
                f"/alerts/{row.alert_id}/resolve",
                json={"resolved_by": "operator-9", "note": "closed"},
            )
            frame = await asyncio.wait_for(queue.get(), timeout=1.0)
        resolved_list = await client.get(
            "/alerts", params={"status": "resolved"}
        )
        active_list = await client.get(
            "/alerts", params={"status": "active"}
        )

    assert response.status_code == 200
    assert frame["kind"] == "alert"
    assert frame["data"]["status"] == "resolved"
    assert [item["alert_id"] for item in resolved_list.json()] == [
        row.alert_id
    ]
    assert active_list.json() == []


async def test_alert_ack_broadcasts_acknowledged_state(db_engine):
    row = await _seed_alert(db_engine)
    app = create_app()
    bus = ObservationBus()
    app.state.bus = bus
    app.state.subscriber = MqttSubscriber(Settings())

    async with _client(app) as client:
        async with bus.subscribe() as queue:
            response = await client.post(
                f"/alerts/{row.alert_id}/ack",
                json={"acknowledged_by": "operator-9", "note": "已处理"},
            )
            frame = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert response.status_code == 200
    assert frame["kind"] == "alert"
    assert frame["data"]["alert_id"] == row.alert_id
    assert frame["data"]["acknowledged_by"] == "operator-9"
    assert frame["data"]["acknowledgement_note"] == "已处理"


# ── Ingest wiring and failure isolation ───────────────────────────────
async def test_alert_hook_persists_before_broadcast(db_engine):
    bus = ObservationBus()
    subscriber = MqttSubscriber(Settings())
    set_subscriber_bus_hook(subscriber, bus, AlertRing())
    alert = make_alert()

    async with bus.subscribe() as queue:
        await subscriber.on_alert(alert)
        frame = await asyncio.wait_for(queue.get(), timeout=1.0)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        rows = await list_alerts(session)

    assert len(rows) == 1
    assert rows[0].alert_id == alert.alert_id
    assert frame["kind"] == "alert"
    assert frame["data"]["alert_id"] == alert.alert_id
    assert frame["data"]["trigger_count"] == 1


async def test_alert_persistence_failure_still_broadcasts(
    db_engine, monkeypatch
):
    async def fail(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repository, "upsert_alert", fail)
    bus = ObservationBus()
    subscriber = MqttSubscriber(Settings())
    set_subscriber_bus_hook(subscriber, bus, AlertRing())
    alert = make_alert()

    async with bus.subscribe() as queue:
        await subscriber.on_alert(alert)
        frame = await asyncio.wait_for(queue.get(), timeout=1.0)

    assert frame["kind"] == "alert"
    assert frame["data"]["alert_id"] == alert.alert_id
    assert frame["data"]["trigger_count"] == 1
