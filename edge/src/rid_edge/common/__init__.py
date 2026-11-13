"""Common utilities: config, logging, ULID, dedup, backoff, geo.

Imported by every other module in the package.
"""
from __future__ import annotations

# Re-export everything that callers need at the package level.
from . import backoff as exponential_backoff  # backoff module
from .backoff import compute_delay, sleep_with_backoff
from .config import Settings, get_settings, reload_settings
from .dedup import Deduper
from .geo import (
    GeoError,
    haversine_distance_m,
    initial_bearing_deg,
    is_zero_coordinate,
)
from .logger import configure_logging, get_logger
from .ulid import new_ulid, timestamp_from_ulid

__all__ = [
    # config
    "Settings",
    "get_settings",
    "reload_settings",
    # logging
    "configure_logging",
    "get_logger",
    # dedup
    "Deduper",
    # ulid
    "new_ulid",
    "timestamp_from_ulid",
    # backoff (also exposed as `exponential_backoff` module)
    "compute_delay",
    "sleep_with_backoff",
    "exponential_backoff",
    # geo
    "GeoError",
    "haversine_distance_m",
    "initial_bearing_deg",
    "is_zero_coordinate",
]
