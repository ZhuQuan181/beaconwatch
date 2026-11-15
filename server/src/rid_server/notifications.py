"""Durable webhook delivery for alert lifecycle events."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import random
import time
from collections import defaultdict
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import orjson
from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased
from ulid import ULID

from .config import Settings
from .db import NotificationOutbox, get_session_factory
from .logging_setup import get_logger

log = get_logger(__name__)

_RETRYABLE_HTTP_STATUSES = frozenset({408, 425, 429})


@dataclass(frozen=True)
class _Delivery:
    delivery_id: str
    alert_id: str
    kind: str
    payload: dict
    attempts: int
    lease_id: str


class NotificationWorker:
    """Claim, sign and deliver alert notifications from the database outbox.

    Delivery is at-least-once.  ``delivery_id`` is stable across retries
    and is sent both in the body and in headers for receiver-side
    de-duplication.  A lease lets a restarted process recover an attempt
    that was interrupted before its result was recorded.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._client = http_client
        self._owns_client = http_client is None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

        # Process-local counters supplement the durable outbox gauges.
        self.delivered = 0
        self.failed = 0
        self.dead = 0
        self.last_success_at: datetime | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self._settings.notifications_enabled or self._task is not None:
            return
        self._stop.clear()
        self._wake.clear()
        self._task = asyncio.create_task(
            self._run(), name="alert-notification-worker"
        )
        log.info(
            "notification.worker.started",
            interval_s=self._settings.notify_worker_interval_s,
            batch_size=self._settings.notify_batch_size,
            concurrency=self._settings.notify_concurrency,
        )

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._task is not None:
            grace = max(2.0, self._settings.notify_webhook_timeout_s + 2.0)
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._task),
                    timeout=grace,
                )
            except TimeoutError:
                self._task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None
        log.info(
            "notification.worker.stopped",
            delivered=self.delivered,
            failed=self.failed,
            dead=self.dead,
        )

    def wake(self) -> None:
        """Ask the loop to poll immediately after a committed enqueue."""
        self._wake.set()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                processed = await self.deliver_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.error(
                    "notification.worker.error",
                    error=type(exc).__name__,
                )
                processed = 0
            if self._stop.is_set():
                break
            if processed:
                continue
            self._wake.clear()
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._settings.notify_worker_interval_s,
                )
            except TimeoutError:
                pass

    async def deliver_once(self) -> int:
        """Claim and process one due batch; return the number claimed."""
        deliveries = await self.claim_due()
        if not deliveries:
            return 0

        by_alert: dict[str, list[_Delivery]] = defaultdict(list)
        for delivery in deliveries:
            by_alert[delivery.alert_id].append(delivery)

        semaphore = asyncio.Semaphore(self._settings.notify_concurrency)

        async def process_group(group: list[_Delivery]) -> None:
            async with semaphore:
                for delivery in group:
                    try:
                        await self._deliver_and_record(delivery)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        # Leave the lease in place.  It expires and another
                        # attempt can recover the row after the process or
                        # this delivery fails internally.
                        log.error(
                            "notification.delivery.internal_error",
                            delivery_id=delivery.delivery_id,
                            alert_id=delivery.alert_id,
                            error=type(exc).__name__,
                        )

        await asyncio.gather(
            *(process_group(group) for group in by_alert.values())
        )
        return len(deliveries)

    def _factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory or get_session_factory()

    async def claim_due(self) -> list[_Delivery]:
        """Lease a due batch without blocking concurrent server workers."""
        now = datetime.now(tz=timezone.utc)
        lease_seconds = max(
            30.0, self._settings.notify_webhook_timeout_s * 3.0
        )
        factory = self._factory()
        async with factory() as session:
            earlier = aliased(NotificationOutbox)
            earlier_pending = (
                select(earlier.delivery_id)
                .where(
                    earlier.alert_id == NotificationOutbox.alert_id,
                    earlier.status.in_(("pending", "delivering")),
                    or_(
                        earlier.created_at < NotificationOutbox.created_at,
                        and_(
                            earlier.created_at
                            == NotificationOutbox.created_at,
                            earlier.delivery_id
                            < NotificationOutbox.delivery_id,
                        ),
                    ),
                )
                .exists()
            )
            stmt = (
                select(NotificationOutbox)
                .where(
                    ~earlier_pending,
                    or_(
                        and_(
                            NotificationOutbox.status == "pending",
                            NotificationOutbox.next_attempt_at <= now,
                        ),
                        and_(
                            NotificationOutbox.status == "delivering",
                            or_(
                                NotificationOutbox.lease_until.is_(None),
                                NotificationOutbox.lease_until <= now,
                            ),
                        ),
                    )
                )
                .order_by(
                    NotificationOutbox.created_at,
                    NotificationOutbox.delivery_id,
                )
                .limit(self._settings.notify_batch_size)
            )
            bind = session.get_bind()
            if bind is not None and bind.dialect.name == "postgresql":
                stmt = stmt.with_for_update(skip_locked=True)
            rows = list((await session.execute(stmt)).scalars().all())
            if not rows:
                return []

            deliveries: list[_Delivery] = []
            for row in rows:
                lease_id = str(ULID())
                row.status = "delivering"
                row.attempts = (row.attempts or 0) + 1
                row.last_attempt_at = now
                row.lease_id = lease_id
                row.lease_until = now + timedelta(seconds=lease_seconds)
                row.updated_at = now
                deliveries.append(
                    _Delivery(
                        delivery_id=row.delivery_id,
                        alert_id=row.alert_id,
                        kind=row.kind,
                        payload=row.payload,
                        attempts=row.attempts,
                        lease_id=lease_id,
                    )
                )
            await session.commit()
            return deliveries

    async def _deliver_and_record(self, delivery: _Delivery) -> None:
        try:
            outcome, detail = await self._post(delivery)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            outcome, detail = "dead", f"internal:{type(exc).__name__}"
        if outcome == "delivered":
            await self._mark_delivered(delivery, detail)
            return
        permanent = outcome == "dead" or (
            delivery.attempts >= self._settings.notify_max_attempts
        )
        await self._mark_failed(delivery, detail, permanent=permanent)

    async def _post(self, delivery: _Delivery) -> tuple[str, str]:
        body = orjson.dumps(delivery.payload)
        timestamp = int(time.time())
        secret = self._settings.notify_webhook_secret
        if secret is None:  # guarded by Settings validation
            raise RuntimeError("webhook signing secret is not configured")
        signature = hmac.new(
            secret.get_secret_value().encode(),
            f"{timestamp}.".encode() + body,
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "BeaconWatch-Notifier/0.9.0",
            "Idempotency-Key": delivery.delivery_id,
            "X-BeaconWatch-Schema-Version": "1",
            "X-BeaconWatch-Event": delivery.kind,
            "X-BeaconWatch-Delivery": delivery.delivery_id,
            "X-BeaconWatch-Timestamp": str(timestamp),
            "X-BeaconWatch-Signature": f"sha256={signature}",
            "X-BeaconWatch-Attempt": str(delivery.attempts),
        }
        try:
            response = await self._ensure_client().post(
                self._settings.notify_webhook_url,
                content=body,
                headers=headers,
            )
        except httpx.TimeoutException:
            return "retry", "timeout"
        except httpx.HTTPError as exc:
            return "retry", type(exc).__name__

        if 200 <= response.status_code < 300:
            return "delivered", str(response.status_code)
        detail = f"HTTP {response.status_code}"
        if (
            response.status_code in _RETRYABLE_HTTP_STATUSES
            or response.status_code >= 500
        ):
            return "retry", detail
        return "dead", detail

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self._settings.notify_webhook_timeout_s
                ),
                follow_redirects=False,
                trust_env=self._settings.notify_webhook_trust_env,
            )
            self._owns_client = True
        return self._client

    def _retry_delay(self, attempts: int) -> float:
        exponent = max(0, attempts - 1)
        ceiling = min(
            self._settings.notify_max_backoff_s,
            self._settings.notify_base_backoff_s * (2**exponent),
        )
        return max(0.1, ceiling * random.uniform(0.5, 1.0))

    async def _mark_delivered(
        self, delivery: _Delivery, status_code: str
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        async with self._factory()() as session:
            result = await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.delivery_id == delivery.delivery_id,
                    NotificationOutbox.status == "delivering",
                    NotificationOutbox.lease_id == delivery.lease_id,
                )
                .values(
                    status="delivered",
                    delivered_at=now,
                    next_attempt_at=now,
                    lease_id=None,
                    lease_until=None,
                    last_error=None,
                    updated_at=now,
                )
            )
            await session.commit()
        if not result.rowcount:
            log.warning(
                "notification.delivered.stale",
                delivery_id=delivery.delivery_id,
            )
            return
        self.delivered += 1
        self.last_success_at = now
        log.info(
            "notification.delivered",
            delivery_id=delivery.delivery_id,
            alert_id=delivery.alert_id,
            kind=delivery.kind,
            status_code=status_code,
            attempt=delivery.attempts,
        )

    async def _mark_failed(
        self, delivery: _Delivery, error: str, *, permanent: bool
    ) -> None:
        now = datetime.now(tz=timezone.utc)
        values: dict[str, object] = {
            "status": "dead" if permanent else "pending",
            "next_attempt_at": (
                now
                if permanent
                else now
                + timedelta(seconds=self._retry_delay(delivery.attempts))
            ),
            "lease_id": None,
            "lease_until": None,
            "last_error": error[:1000],
            "updated_at": now,
            "failures": NotificationOutbox.failures + 1,
        }
        async with self._factory()() as session:
            result = await session.execute(
                update(NotificationOutbox)
                .where(
                    NotificationOutbox.delivery_id == delivery.delivery_id,
                    NotificationOutbox.status == "delivering",
                    NotificationOutbox.lease_id == delivery.lease_id,
                )
                .values(**values)
            )
            await session.commit()
        if not result.rowcount:
            log.warning(
                "notification.failed.stale",
                delivery_id=delivery.delivery_id,
            )
            return
        self.failed += 1
        if permanent:
            self.dead += 1
        log.warning(
            "notification.dead" if permanent else "notification.retry",
            delivery_id=delivery.delivery_id,
            alert_id=delivery.alert_id,
            kind=delivery.kind,
            attempt=delivery.attempts,
            error=error,
        )
