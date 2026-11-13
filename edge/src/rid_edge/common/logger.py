"""Structured logging for the edge station.

We use `structlog` so the same logger produces:
- pretty colored output on a TTY (dev mode)
- JSON lines on systemd / containerd (no parsing, journald-friendly)

The edge station may run unattended for weeks; logs MUST include
context (station_id, drone_id, sequence) for post-mortem correlation.
"""
from __future__ import annotations

import logging
import sys

import structlog

from rid_edge.common.config import get_settings


def configure_logging() -> None:
    """Idempotent global log setup. Safe to call from main() and tests."""
    settings = get_settings()

    # Standard library logging: route everything through structlog.
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, settings.log_level),
    )

    shared_processors: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
    ]

    if settings.log_json or not sys.stdout.isatty():
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, settings.log_level)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Bind station_id once so every log line carries it.
    structlog.contextvars.bind_contextvars(station_id=settings.station_id)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a logger bound to `name` (usually __name__)."""
    return structlog.get_logger(name)
