"""Durable alert ledger, acknowledgement and real-time fallback tests."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
from rid_server import repository
from rid_server.alerts import Alert, AlertRing
from rid_server.app import create_app
from rid_server.bus import ObservationBus
from rid_server.config import Settings
from rid_server.mqtt_subscriber import MqttSubscriber
from rid_server.repository import (
    acknowledge_alert,
    list_alerts,
    upsert_alert,
)
from rid_server.settings_state import set_subscriber_bus_hook
from sqlalchemy.ext.asyncio import async_sessionmaker


def make_alert(
    *,
    alert_id: str = "altitude-abc123",
    at: datetime | None = None,
    message: str = "测试告警",
) -> Alert:
    observed_at = at or datetime(2026, 11, 15, 8, 0, tzinfo=timezone.utc)
    return Alert(
        alert_id=alert_id,
        rule="altitude",
        severity="warn",
        drone_id="drone-A",
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
