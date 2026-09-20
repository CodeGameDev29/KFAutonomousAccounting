"""Shared utilities for integration sync modules (Wise, PayPal, Amazon, etc.).

Holds the HTTP client, the stable row hash and the calendar-month helpers in
one place, so every sync module derives dedup keys and month boundaries the
same way.
"""

from __future__ import annotations

import calendar
import hashlib
import ssl
from datetime import date

import certifi
import httpx

_PG_BIGINT_MAX = (1 << 63) - 1


def stable_hash(s: str) -> int:
    """Return a stable integer hash of a string, consistent across process restarts.

    Uses first 15 hex chars of MD5 (60 bits), capped at PostgreSQL BIGINT max
    (2^63 - 1 = 9,223,372,036,854,775,807) for safe DB insertion.
    """
    return int(hashlib.md5(s.encode()).hexdigest()[:15], 16) & _PG_BIGINT_MAX


def month_range(
    start: tuple[int, int], end: tuple[int, int]
) -> list[tuple[int, int]]:
    """Generate list of (year, month) tuples from start to end inclusive."""
    months: list[tuple[int, int]] = []
    y, m = start
    while (y, m) <= end:
        months.append((y, m))
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return months


def last_day_of_month(year: int, month: int) -> date:
    """Return the last day of the given month."""
    _, last_day = calendar.monthrange(year, month)
    return date(year, month, last_day)


def get_http_client(timeout: int = 30) -> httpx.Client:
    """Create an httpx client with proper SSL via certifi."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    return httpx.Client(verify=ssl_context, timeout=timeout)
