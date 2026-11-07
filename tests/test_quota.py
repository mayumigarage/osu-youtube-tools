from datetime import datetime, timezone

import pytest

from osu2yt import db
from osu2yt.youtube import quota


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "now, date",
    [
        (utc(2026, 11, 6, 7, 59), "2026-11-05"),   # PST (UTC-8): still the 5th in California
        (utc(2026, 11, 6, 8, 0), "2026-11-06"),
        (utc(2026, 7, 1, 6, 59), "2026-06-30"),    # PDT (UTC-7)
        (utc(2026, 7, 1, 7, 0), "2026-07-01"),
    ],
)
def test_pt_date_boundaries(now, date):
    assert quota.pt_date(now) == date


def test_next_reset_is_pacific_midnight():
    reset = quota.next_reset(utc(2026, 11, 6, 3, 0))  # = Nov 5 19:00 PST
    assert reset.astimezone(timezone.utc) == utc(2026, 11, 6, 8, 0)
    reset = quota.next_reset(utc(2026, 11, 6, 9, 0))  # = Nov 6 01:00 PST
    assert reset.astimezone(timezone.utc) == utc(2026, 11, 7, 8, 0)


def test_usage_is_grouped_by_pacific_day(conn):
    yesterday, today = utc(2026, 11, 6, 7, 0), utc(2026, 11, 6, 9, 0)
    quota.record(conn, "playlistItems.insert", now=yesterday)
    quota.record(conn, "playlistItems.insert", now=today)
    quota.record(conn, "videos.list", now=today)
    assert quota.used(conn, today) == 51
    assert quota.used(conn, yesterday) == 50
    assert quota.breakdown(conn, today) == {"playlistItems.insert": 50, "videos.list": 1}


def test_status_and_budget(conn):
    now = utc(2026, 11, 6, 9, 0)
    st = quota.status(conn, now)
    assert (st.used, st.limit, st.margin, st.available) == (0, 10_000, 300, 9_700)
    assert st.inserts_possible == 194
    for _ in range(190):
        quota.record(conn, "playlistItems.insert", now=now)
    assert quota.status(conn, now).inserts_possible == 4
    assert quota.can_spend(conn, 200, now)
    assert not quota.can_spend(conn, 201, now)


def test_limits_from_settings(conn):
    db.set_setting(conn, "quota_limit", "20000")
    db.set_setting(conn, "quota_margin", "0")
    assert quota.status(conn).available == 20_000
