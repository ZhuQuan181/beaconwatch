"""Geo helpers used by edge code.

Pure functions; no I/O.  All inputs validated.
"""
from __future__ import annotations

import math

# Mean Earth radius (WGS-84).  Good enough for drone-scale distances.
_EARTH_RADIUS_M = 6_371_008.8


class GeoError(ValueError):
    """Invalid coordinate input."""


def _check(lat: float, lon: float) -> None:
    if not -90.0 <= lat <= 90.0:
        raise GeoError(f"latitude out of range: {lat}")
    if not -180.0 <= lon <= 180.0:
        raise GeoError(f"longitude out of range: {lon}")


def haversine_distance_m(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Great-circle distance in metres between two WGS-84 points."""
    _check(lat1, lon1)
    _check(lat2, lon2)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2.0) ** 2
    )
    return _EARTH_RADIUS_M * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def initial_bearing_deg(
    lat1: float, lon1: float, lat2: float, lon2: float
) -> float:
    """Initial bearing in degrees [0, 360) from p1 to p2."""
    _check(lat1, lon1)
    _check(lat2, lon2)
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dlam = math.radians(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = (
        math.cos(phi1) * math.sin(phi2)
        - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    )
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def is_zero_coordinate(lat: float, lon: float) -> bool:
    """True if (lat, lon) is the (0, 0) 'no-fix' sentinel."""
    return abs(lat) < 1e-6 and abs(lon) < 1e-6


__all__ = ["GeoError", "haversine_distance_m", "initial_bearing_deg", "is_zero_coordinate"]
