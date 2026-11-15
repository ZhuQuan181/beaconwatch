"""Durable notification outbox and webhook worker tests."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from rid_server import repository
from rid_server.alerts import Alert
from rid_server.app import create_app
from rid_server.bus import ObservationBus
from rid_server.config import Settings
from rid_server.db import NotificationOutbox
from rid_server.mqtt_subscriber import MqttSubscriber
from rid_server.notifications import NotificationWorker
from rid_server.repository import (
    acknowledge_alert,
    list_alert_events,
    notification_outbox_counts,
    reopen_alert,
    resolve_alert,
    upsert_alert,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker


def make_alert(
    *,
    alert_id: str = "altitude-notify",
    at: datetime | None = None,
    drone_id: str = "drone-N",
) -> Alert:
    observed_at = at or datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
    return Alert(
        alert_id=alert_id,
        rule="altitude",
        severity="warn",
        drone_id=drone_id,
        message="测试通知告警",
        detail={"alt_m": 180.0, "limit_m": 120.0},
        at=observed_at.isoformat(),
    )


def make_settings(**overrides) -> Settings:
    values = {
        "notify_webhook_url": "https://hooks.example.test/alerts",
        "notify_webhook_secret": "test-webhook-secret-32-bytes",
        "notify_webhook_timeout_s": 2.0,
        "notify_worker_interval_s": 0.1,
        "notify_batch_size": 20,
        "notify_concurrency": 2,
        "notify_max_attempts": 3,
        "notify_base_backoff_s": 0.1,
        "notify_max_backoff_s": 1.0,
    }
    values.update(overrides)
    return Settings(**values)


def factory_for(db_engine):
    return async_sessionmaker(db_engine, expire_on_commit=False)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def get_outbox(factory) -> list[NotificationOutbox]:
    async with factory() as session:
        result = await session.execute(
            select(NotificationOutbox).order_by(
                NotificationOutbox.created_at,
                NotificationOutbox.delivery_id,
            )
        )
        return list(result.scalars().all())


async def force_due(factory, delivery_id: str) -> None:
    async with factory() as session:
        row = await session.get(NotificationOutbox, delivery_id)
        assert row is not None
        row.next_attempt_at = datetime.now(tz=timezone.utc) - timedelta(
            seconds=1
        )
        await session.commit()


def mock_worker(
    settings: Settings,
    factory,
    handler,
) -> tuple[NotificationWorker, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    worker = NotificationWorker(
        settings,
        session_factory=factory,
        http_client=client,
    )
    return worker, client


# ── Enqueue and lifecycle coverage ────────────────────────────────────
async def test_trigger_enqueues_durable_notification(db_engine, session):
    alert = make_alert()
    await upsert_alert(
        session,
        alert,
        enqueue_notification=True,
    )
    await session.commit()

    rows = await get_outbox(factory_for(db_engine))
    assert len(rows) == 1
    outbox = rows[0]
    assert outbox.alert_id == alert.alert_id
    assert outbox.kind == "alert.triggered"
    assert outbox.status == "pending"
    assert outbox.attempts == 0
    assert outbox.failures == 0
    assert outbox.payload["schema"] == "beaconwatch.alert-notification.v1"
    assert outbox.payload["delivery_id"] == outbox.delivery_id
    assert outbox.payload["event"] == "alert.triggered"
    assert outbox.payload["alert"]["alert_id"] == alert.alert_id
    assert outbox.payload["metadata"] == {
        "recurrence": False,
        "automatic": False,
        "previous_status": None,
        "trigger_count": 1,
        "reopen_count": 0,
    }


async def test_lifecycle_transitions_enqueue_once(session):
    first_at = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
    await upsert_alert(
        session, make_alert(at=first_at), enqueue_notification=True
    )
    await acknowledge_alert(
        session,
        "altitude-notify",
        acknowledged_by="alice",
        note="已派发",
        now=first_at + timedelta(minutes=1),
        enqueue_notification=True,
    )
    await acknowledge_alert(
        session,
        "altitude-notify",
        acknowledged_by="retry",
        now=first_at + timedelta(minutes=2),
        enqueue_notification=True,
    )
    await resolve_alert(
        session,
        "altitude-notify",
        resolved_by="bob",
        note="处置完成",
        now=first_at + timedelta(minutes=3),
        enqueue_notification=True,
    )
    await resolve_alert(
        session,
        "altitude-notify",
        resolved_by="retry",
        now=first_at + timedelta(minutes=4),
        enqueue_notification=True,
    )
    await reopen_alert(
        session,
        "altitude-notify",
        reopened_by="carol",
        note="复飞",
        now=first_at + timedelta(minutes=5),
        enqueue_notification=True,
    )
    await resolve_alert(
        session,
        "altitude-notify",
        resolved_by="carol",
        note="最终关闭",
        now=first_at + timedelta(minutes=6),
        enqueue_notification=True,
    )

    rows = await get_outbox(
        async_sessionmaker(session.bind, expire_on_commit=False)
    )
    assert [row.kind for row in rows] == [
        "alert.triggered",
        "alert.acknowledged",
        "alert.resolved",
        "alert.reopened",
        "alert.resolved",
    ]
    assert rows[1].payload["actor"] == "alice"
    assert rows[1].payload["note"] == "已派发"
    assert rows[3].payload["metadata"]["previous_status"] == "resolved"


async def test_resolved_recurrence_enqueues_automatic_reopen(session):
    first_at = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
    await upsert_alert(session, make_alert(at=first_at))
    await resolve_alert(
        session,
        "altitude-notify",
        resolved_by="alice",
        now=first_at + timedelta(minutes=1),
    )

    await upsert_alert(
        session,
        make_alert(at=first_at + timedelta(minutes=3)),
        enqueue_notification=True,
    )

    rows = await get_outbox(
        async_sessionmaker(session.bind, expire_on_commit=False)
    )
    assert len(rows) == 1
    assert rows[0].kind == "alert.reopened"
    assert rows[0].payload["metadata"]["automatic"] is True
    assert rows[0].payload["metadata"]["previous_status"] == "resolved"
    assert rows[0].payload["metadata"]["trigger_count"] == 2


async def test_outbox_failure_does_not_lose_alert_or_audit(
    session, monkeypatch
):
    class BrokenOutbox:
        def __init__(self, **_kwargs) -> None:
            raise RuntimeError("outbox unavailable")

    monkeypatch.setattr(repository, "NotificationOutbox", BrokenOutbox)
    row = await upsert_alert(
        session,
        make_alert(),
        enqueue_notification=True,
    )

    assert row.alert_id == "altitude-notify"
    assert [event.action for event in await list_alert_events(
        session, row.alert_id
    )] == ["triggered"]
    assert await get_outbox(
        async_sessionmaker(session.bind, expire_on_commit=False)
    ) == []


# ── Worker delivery and retry semantics ───────────────────────────────
async def test_worker_sends_signed_webhook_and_marks_delivered(
    db_engine, session
):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    factory = factory_for(db_engine)
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["headers"] = dict(request.headers)
        captured["body"] = request.content
        return httpx.Response(204)

    settings = make_settings()
    worker, client = mock_worker(settings, factory, handler)
    try:
        assert await worker.deliver_once() == 1
    finally:
        await client.aclose()

    rows = await get_outbox(factory)
    assert len(rows) == 1
    assert rows[0].status == "delivered"
    assert rows[0].attempts == 1
    assert rows[0].failures == 0
    assert rows[0].delivered_at is not None
    assert rows[0].lease_id is None
    assert rows[0].lease_until is None

    headers = captured["headers"]
    body = captured["body"]
    assert isinstance(headers, dict)
    assert isinstance(body, bytes)
    timestamp = headers["x-beaconwatch-timestamp"]
    expected = hmac.new(
        settings.notify_webhook_secret.get_secret_value().encode(),
        f"{timestamp}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    assert headers["x-beaconwatch-signature"] == f"sha256={expected}"
    assert headers["x-beaconwatch-delivery"] == rows[0].delivery_id
    assert headers["x-beaconwatch-event"] == "alert.triggered"
    assert headers["x-beaconwatch-attempt"] == "1"
    assert headers["idempotency-key"] == rows[0].delivery_id
    assert rows[0].delivery_id.encode() in body
    assert worker.delivered == 1
    assert worker.last_success_at is not None


async def test_background_worker_dispatches_without_manual_tick(
    db_engine, session
):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    factory = factory_for(db_engine)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    worker, client = mock_worker(
        make_settings(notify_worker_interval_s=0.1),
        factory,
        handler,
    )
    await worker.start()
    try:
        for _ in range(30):
            if (await get_outbox(factory))[0].status == "delivered":
                break
            await asyncio.sleep(0.05)
        assert worker.running is True
    finally:
        await worker.stop()
        await client.aclose()

    assert (await get_outbox(factory))[0].status == "delivered"
    assert worker.running is False


async def test_stop_waits_for_inflight_delivery_to_finish(
    db_engine, session
):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    factory = factory_for(db_engine)
    started = asyncio.Event()

    class SlowTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, _request: httpx.Request
        ) -> httpx.Response:
            started.set()
            await asyncio.sleep(0.2)
            return httpx.Response(204)

    client = httpx.AsyncClient(transport=SlowTransport())
    worker = NotificationWorker(
        make_settings(notify_worker_interval_s=0.1),
        session_factory=factory,
        http_client=client,
    )
    await worker.start()
    try:
        await asyncio.wait_for(started.wait(), timeout=1.0)
        await worker.stop()
    finally:
        await client.aclose()

    row = (await get_outbox(factory))[0]
    assert row.status == "delivered"
    assert row.lease_id is None
    assert worker.delivered == 1


async def test_5xx_retries_then_succeeds(db_engine, session):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    factory = factory_for(db_engine)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 204)

    settings = make_settings(notify_max_attempts=4)
    worker, client = mock_worker(settings, factory, handler)
    try:
        assert await worker.deliver_once() == 1
        first = (await get_outbox(factory))[0]
        assert first.status == "pending"
        assert first.attempts == 1
        assert first.failures == 1
        assert first.last_error == "HTTP 503"
        assert as_utc(first.next_attempt_at) > datetime.now(tz=timezone.utc)

        await force_due(factory, first.delivery_id)
        assert await worker.deliver_once() == 1
    finally:
        await client.aclose()

    final = (await get_outbox(factory))[0]
    assert final.status == "delivered"
    assert final.attempts == 2
    assert final.failures == 1
    assert final.last_error is None
    assert calls == 2


async def test_later_events_wait_for_earlier_event(db_engine, session):
    first_at = datetime(2026, 11, 16, 2, 0, tzinfo=timezone.utc)
    await upsert_alert(
        session,
        make_alert(at=first_at),
        enqueue_notification=True,
    )
    await acknowledge_alert(
        session,
        "altitude-notify",
        acknowledged_by="alice",
        now=first_at + timedelta(minutes=1),
        enqueue_notification=True,
    )
    factory = factory_for(db_engine)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503 if calls == 1 else 204)

    worker, client = mock_worker(
        make_settings(notify_max_attempts=3),
        factory,
        handler,
    )
    try:
        assert await worker.deliver_once() == 1
        after_failure = await get_outbox(factory)
        assert after_failure[0].status == "pending"
        assert after_failure[1].status == "pending"
        assert after_failure[1].attempts == 0
        assert await worker.deliver_once() == 0

        await force_due(factory, after_failure[0].delivery_id)
        assert await worker.deliver_once() == 1
        assert await worker.deliver_once() == 1
    finally:
        await client.aclose()

    final = await get_outbox(factory)
    assert [row.status for row in final] == ["delivered", "delivered"]
    assert calls == 3


async def test_permanent_4xx_goes_dead_without_retry(db_engine, session):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    factory = factory_for(db_engine)
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403)

    worker, client = mock_worker(make_settings(), factory, handler)
    try:
        assert await worker.deliver_once() == 1
        assert await worker.deliver_once() == 0
    finally:
        await client.aclose()

    row = (await get_outbox(factory))[0]
    assert row.status == "dead"
    assert row.attempts == 1
    assert row.failures == 1
    assert row.last_error == "HTTP 403"
    assert calls == 1
    assert worker.dead == 1


async def test_max_attempts_moves_notification_to_dead(db_engine, session):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    factory = factory_for(db_engine)

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    settings = make_settings(notify_max_attempts=2)
    worker, client = mock_worker(settings, factory, handler)
    try:
        await worker.deliver_once()
        retry = (await get_outbox(factory))[0]
        assert retry.status == "pending"
        await force_due(factory, retry.delivery_id)
        await worker.deliver_once()
    finally:
        await client.aclose()

    row = (await get_outbox(factory))[0]
    assert row.status == "dead"
    assert row.attempts == 2
    assert row.failures == 2
    assert row.last_error == "HTTP 500"


async def test_timeout_is_retryable(db_engine, session):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    factory = factory_for(db_engine)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    worker, client = mock_worker(make_settings(), factory, handler)
    try:
        await worker.deliver_once()
    finally:
        await client.aclose()

    row = (await get_outbox(factory))[0]
    assert row.status == "pending"
    assert row.failures == 1
    assert row.last_error == "timeout"


async def test_expired_delivery_lease_is_recovered(db_engine, session):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    factory = factory_for(db_engine)
    row = (await get_outbox(factory))[0]
    async with factory() as write_session:
        stored = await write_session.get(NotificationOutbox, row.delivery_id)
        assert stored is not None
        stored.status = "delivering"
        stored.attempts = 1
        stored.lease_id = "crashed-worker"
        stored.lease_until = datetime.now(tz=timezone.utc) - timedelta(
            seconds=5
        )
        await write_session.commit()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(202)

    worker, client = mock_worker(make_settings(), factory, handler)
    try:
        assert await worker.deliver_once() == 1
    finally:
        await client.aclose()

    recovered = (await get_outbox(factory))[0]
    assert recovered.status == "delivered"
    assert recovered.attempts == 2


async def test_active_lease_is_not_double_claimed(db_engine, session):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    factory = factory_for(db_engine)
    row = (await get_outbox(factory))[0]
    async with factory() as write_session:
        stored = await write_session.get(NotificationOutbox, row.delivery_id)
        assert stored is not None
        stored.status = "delivering"
        stored.attempts = 1
        stored.lease_id = "live-worker"
        stored.lease_until = datetime.now(tz=timezone.utc) + timedelta(
            minutes=5
        )
        await write_session.commit()

    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an active lease must not be claimed")

    worker, client = mock_worker(make_settings(), factory, handler)
    try:
        assert await worker.deliver_once() == 0
    finally:
        await client.aclose()


async def test_worker_does_not_start_without_webhook_configuration():
    worker = NotificationWorker(Settings(mqtt_host="test"))
    await worker.start()
    assert worker.running is False
    await worker.stop()


async def test_webhook_client_ignores_invalid_global_proxy(monkeypatch):
    monkeypatch.setenv("ALL_PROXY", "socks://127.0.0.1:7897")
    worker = NotificationWorker(make_settings())
    client = worker._ensure_client()
    assert client is not None
    await worker.stop()


async def test_outbox_counts_include_retry_and_dead_state(
    db_engine, session
):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    factory = factory_for(db_engine)
    row = (await get_outbox(factory))[0]
    async with factory() as write_session:
        stored = await write_session.get(NotificationOutbox, row.delivery_id)
        assert stored is not None
        stored.status = "dead"
        stored.attempts = 3
        stored.failures = 2
        await write_session.commit()
        counts = await notification_outbox_counts(write_session)

    assert counts["dead"] == 1
    assert counts["pending"] == 0
    assert counts["attempts"] == 3
    assert counts["failures"] == 2


# ── API wiring / configuration ────────────────────────────────────────
def http_client_for(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://t",
    )


async def test_alert_api_enqueues_lifecycle_notifications(db_engine, session):
    row = await upsert_alert(session, make_alert())
    app = create_app()
    app.state.settings = make_settings()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(Settings(mqtt_host="test"))

    async with http_client_for(app) as client:
        acknowledged = await client.post(
            f"/alerts/{row.alert_id}/ack",
            json={"acknowledged_by": "operator-1", "note": "ack"},
        )
        resolved = await client.post(
            f"/alerts/{row.alert_id}/resolve",
            json={"resolved_by": "operator-2", "note": "resolved"},
        )
        reopened = await client.post(
            f"/alerts/{row.alert_id}/reopen",
            json={"reopened_by": "operator-3", "note": "reopen"},
        )

    assert acknowledged.status_code == 200
    assert resolved.status_code == 200
    assert reopened.status_code == 200
    rows = await get_outbox(factory_for(db_engine))
    assert [item.kind for item in rows] == [
        "alert.acknowledged",
        "alert.resolved",
        "alert.reopened",
    ]
    assert [item.payload["actor"] for item in rows] == [
        "operator-1",
        "operator-2",
        "operator-3",
    ]


async def test_metrics_expose_durable_outbox_state(db_engine, session):
    await upsert_alert(
        session, make_alert(), enqueue_notification=True
    )
    await upsert_alert(
        session,
        make_alert(alert_id="altitude-delivered"),
        enqueue_notification=True,
    )
    await upsert_alert(
        session,
        make_alert(alert_id="altitude-dead"),
        enqueue_notification=True,
    )
    factory = factory_for(db_engine)
    rows = await get_outbox(factory)
    async with factory() as write_session:
        delivered = await write_session.get(
            NotificationOutbox, rows[1].delivery_id
        )
        dead = await write_session.get(
            NotificationOutbox, rows[2].delivery_id
        )
        assert delivered is not None
        assert dead is not None
        delivered.status = "delivered"
        delivered.attempts = 1
        dead.status = "dead"
        dead.attempts = 2
        dead.failures = 1
        await write_session.commit()

    app = create_app()
    app.state.bus = ObservationBus()
    app.state.subscriber = MqttSubscriber(Settings(mqtt_host="test"))

    async with http_client_for(app) as client:
        response = await client.get("/metrics")

    assert response.status_code == 200
    assert "rid_notification_outbox_pending 1" in response.text
    assert "rid_notification_outbox_delivered 1" in response.text
    assert "rid_notification_outbox_dead 1" in response.text
    assert "rid_notification_outbox_attempts_total 3" in response.text
    assert "rid_notification_outbox_failures_total 1" in response.text
    assert "rid_notification_worker_running 0" in response.text


def test_webhook_configuration_requires_url_secret_pair():
    with pytest.raises(ValueError, match="WEBHOOK_SECRET"):
        Settings(notify_webhook_url="https://hooks.example.test/alerts")
    with pytest.raises(ValueError, match="absolute http"):
        Settings(
            notify_webhook_url="file:///tmp/hook",
            notify_webhook_secret="secret",
        )
