"""Exponential backoff with jitter for network reconnect loops."""
from __future__ import annotations

import asyncio
import random


def compute_delay(
    attempt: int,
    base_s: float = 0.5,
    cap_s: float = 30.0,
    jitter: float = 0.1,
) -> float:
    """Return sleep seconds for the given retry attempt (0-indexed).

    attempt=0 → ~base_s, attempt=1 → ~2*base_s, ..., capped at cap_s.
    Adds full jitter in [1-jitter, 1+jitter] to avoid thundering herd.
    """
    if attempt < 0:
        attempt = 0
    delay = min(cap_s, base_s * (2 ** attempt))
    if jitter > 0:
        delay *= 1.0 + (random.random() * 2 - 1) * jitter
    return max(0.0, delay)


async def sleep_with_backoff(attempt: int, base_s: float = 0.5, cap_s: float = 30.0) -> float:
    """Sleep for compute_delay(attempt) and return the actual delay used."""
    delay = compute_delay(attempt, base_s, cap_s)
    await asyncio.sleep(delay)
    return delay
