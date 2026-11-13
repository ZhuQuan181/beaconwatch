"""ULID generation.

ULID (Universally Unique Lexicographically Sortable Identifier) is a 128-bit
identifier that is:
- Sortable by creation time (lexicographic order matches chronological order)
- URL-safe (Crockford base32)
- Collision-resistant (80 bits of randomness + 48-bit timestamp)

We use it as the `observation_id` so that the server can dedup trivially and
the time-ordering helps when querying recent observations.
"""
from __future__ import annotations

import os
import time

# Crockford base32 alphabet (no I, L, O, U to avoid confusion)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_ENCODING = _CROCKFORD.encode()


def _encode_base32(value: int, length: int) -> str:
    """Encode an integer as base32 (Crockford alphabet), MSB first, zero-padded."""
    out = bytearray(length)
    for i in range(length - 1, -1, -1):
        out[i] = _ENCODING[value & 0x1F]
        value >>= 5
    return out.decode("ascii")


def new_ulid() -> str:
    """Generate a new ULID string.

    Format: 10 chars timestamp (ms since epoch) + 16 chars randomness = 26 chars.

    We avoid the `python-ulid` dependency to keep the edge image lean and to
    guarantee no network/PyPI failure on a freshly-flashed industrial PC.
    """
    # 48-bit millisecond timestamp
    ts_ms = int(time.time() * 1000) & ((1 << 48) - 1)

    # 80 bits of randomness: 10 bytes from os.urandom
    rand_bytes = os.urandom(10)
    rand_int = int.from_bytes(rand_bytes, "big")

    return _encode_base32(ts_ms, 10) + _encode_base32(rand_int, 16)


def timestamp_from_ulid(ulid: str) -> int:
    """Extract the embedded millisecond timestamp from a ULID."""
    ts_chars = ulid[:10]
    ts = 0
    for c in ts_chars:
        # Crockford decoding accepts upper and lower case
        idx = _CROCKFORD.find(c.upper())
        if idx < 0:
            raise ValueError(f"invalid ULID char: {c!r}")
        ts = (ts << 5) | idx
    return ts
