"""YouTube Data API quota bookkeeping.

The daily quota resets at midnight Pacific Time, so usage is grouped by the PT date.
Units are recorded per request attempt (failed requests are charged too).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..db import get_setting, now_iso

PACIFIC = ZoneInfo("America/Los_Angeles")

COSTS = {
    "channels.list": 1,
    "playlists.list": 1,
    "playlists.insert": 50,
    "playlistItems.list": 1,
    "playlistItems.insert": 50,
    "playlistItems.delete": 50,
    "videos.list": 1,
}

DEFAULT_LIMIT = 10_000
DEFAULT_MARGIN = 300  # keep some units for listing / checks / mistakes


def pt_date(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(PACIFIC).date().isoformat()


def next_reset(now: datetime | None = None) -> datetime:
    """Next midnight Pacific Time, as an aware datetime (convert with .astimezone() for display)."""
    now = (now or datetime.now(timezone.utc)).astimezone(PACIFIC)
    tomorrow = (now + timedelta(days=1)).date()
    return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=PACIFIC)


def record(conn: sqlite3.Connection, method: str, units: int | None = None, now: datetime | None = None) -> None:
    units = COSTS[method] if units is None else units
    with conn:
        conn.execute(
            "INSERT INTO quota_log(ts, pt_date, method, units) VALUES(?, ?, ?, ?)",
            (now_iso(), pt_date(now), method, units),
        )


def used(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    row = conn.execute("SELECT COALESCE(SUM(units), 0) FROM quota_log WHERE pt_date = ?", (pt_date(now),)).fetchone()
    return int(row[0])


def breakdown(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, int]:
    rows = conn.execute(
        "SELECT method, SUM(units) FROM quota_log WHERE pt_date = ? GROUP BY method ORDER BY 2 DESC", (pt_date(now),)
    ).fetchall()
    return {r[0]: int(r[1]) for r in rows}


def limits(conn: sqlite3.Connection) -> tuple[int, int]:
    limit = int(get_setting(conn, "quota_limit") or DEFAULT_LIMIT)
    margin = int(get_setting(conn, "quota_margin") or DEFAULT_MARGIN)
    return limit, margin


@dataclass
class QuotaStatus:
    pt_date: str
    used: int
    limit: int
    margin: int
    reset_at: datetime

    @property
    def available(self) -> int:
        """Units still usable today after keeping the safety margin."""
        return max(0, self.limit - self.margin - self.used)

    @property
    def inserts_possible(self) -> int:
        return self.available // COSTS["playlistItems.insert"]


def status(conn: sqlite3.Connection, now: datetime | None = None) -> QuotaStatus:
    limit, margin = limits(conn)
    return QuotaStatus(pt_date(now), used(conn, now), limit, margin, next_reset(now))


def can_spend(conn: sqlite3.Connection, units: int, now: datetime | None = None) -> bool:
    return status(conn, now).available >= units
