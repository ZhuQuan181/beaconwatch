"""Opt-in retention cleanup for alert history and notification outbox rows."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import Settings
from .db import get_session_factory
from .logging_setup import get_logger
from .repository import (
    purge_expired_notifications,
    purge_expired_resolved_alerts,
)

log = get_logger(__name__)


class RetentionWorker:
    """Run bounded cleanup batches without blocking ingest or shutdown.

    Both policies default to disabled in :class:`Settings`.  An alert is
    removed only after it has been resolved for the configured period and
    has no pending or in-flight notification.  Notification rows are
    removed only from terminal states.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.alerts_deleted = 0
        self.notifications_deleted = 0
        self.cycles = 0
        self.failures = 0
        self.last_success_at: datetime | None = None
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return (
            self._settings.alert_retention_days > 0
            or self._settings.notification_retention_days > 0
        )

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(
            self._run(), name="alert-retention-worker"
        )
        log.info(
            "retention.worker.started",
            alert_days=self._settings.alert_retention_days,
            notification_days=self._settings.notification_retention_days,
            interval_s=self._settings.retention_interval_s,
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(self._task),
                timeout=10.0,
            )
        except TimeoutError:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        except (asyncio.CancelledError, Exception):
            pass
        self._task = None
        log.info(
            "retention.worker.stopped",
            alerts_deleted=self.alerts_deleted,
            notifications_deleted=self.notifications_deleted,
            failures=self.failures,
        )

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.failures += 1
                self.last_error = type(exc).__name__
                log.error(
                    "retention.worker.error",
                    error=type(exc).__name__,
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self._settings.retention_interval_s,
                )
            except TimeoutError:
                pass

    async def run_once(self) -> None:
        """Run one cleanup cycle, committing at most one batch at a time."""
        if not self.enabled:
            return
        now = datetime.now(tz=timezone.utc)
        factory = self._session_factory or get_session_factory()
        if self._settings.alert_retention_days > 0:
            older_than = now - timedelta(
                days=self._settings.alert_retention_days
            )
            while True:
                async with factory() as session:
                    deleted = await purge_expired_resolved_alerts(
                        session,
                        older_than=older_than,
                        batch_size=self._settings.retention_batch_size,
                    )
                self.alerts_deleted += deleted
                if deleted < self._settings.retention_batch_size:
                    break
        if self._settings.notification_retention_days > 0:
            older_than = now - timedelta(
                days=self._settings.notification_retention_days
            )
            while True:
                async with factory() as session:
                    deleted = await purge_expired_notifications(
                        session,
                        older_than=older_than,
                        batch_size=self._settings.retention_batch_size,
                    )
                self.notifications_deleted += deleted
                if deleted < self._settings.retention_batch_size:
                    break
        self.cycles += 1
        self.last_success_at = datetime.now(tz=timezone.utc)
        self.last_error = None
