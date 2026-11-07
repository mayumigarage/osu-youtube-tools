"""Playlist bookkeeping in SQLite on top of YouTubeClient."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..db import get_setting, now_iso, set_setting
from .client import PLAYABLE, Playlist, YouTubeClient

ACTIVE_KEY = "active_playlist_id"


def upsert_playlists(conn: sqlite3.Connection, playlists: list[Playlist]) -> None:
    with conn:
        for p in playlists:
            conn.execute(
                """INSERT INTO playlists(id, title, privacy, created_by_tool, created_at, item_count)
                   VALUES(?, ?, ?, 0, NULL, ?)
                   ON CONFLICT(id) DO UPDATE SET title=excluded.title, privacy=excluded.privacy,
                                                 item_count=excluded.item_count""",
                (p.id, p.title, p.privacy, p.item_count),
            )


def refresh_my_playlists(conn: sqlite3.Connection, client: YouTubeClient) -> list[Playlist]:
    playlists = client.list_my_playlists()
    upsert_playlists(conn, playlists)
    return playlists


def create(conn: sqlite3.Connection, client: YouTubeClient, title: str, privacy: str = "private",
           description: str = "", make_active: bool = True) -> Playlist:
    p = client.create_playlist(title, privacy, description)
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO playlists(id, title, privacy, created_by_tool, created_at, item_count) "
            "VALUES(?, ?, ?, 1, ?, 0)",
            (p.id, p.title, p.privacy, now_iso()),
        )
    if make_active:
        set_active(conn, p.id)
    return p


def set_active(conn: sqlite3.Connection, playlist_id: str) -> None:
    set_setting(conn, ACTIVE_KEY, playlist_id)


def active(conn: sqlite3.Connection) -> sqlite3.Row | None:
    pid = get_setting(conn, ACTIVE_KEY)
    if not pid:
        return None
    row = conn.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
    if row is None:  # chosen before its metadata was stored
        with conn:
            conn.execute("INSERT OR IGNORE INTO playlists(id, title) VALUES(?, ?)", (pid, pid))
        row = conn.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
    return row


@dataclass
class PullResult:
    total: int
    new_remote: int        # videos found in the playlist that we did not know about
    went_missing: int      # known videos no longer in the playlist
    duplicates: int        # same video more than once in the playlist


def pull_items(conn: sqlite3.Connection, client: YouTubeClient, playlist_id: str) -> PullResult:
    """Mirror the playlist's current contents into playlist_items (costs 1 unit per 50 items)."""
    items = client.list_playlist_items(playlist_id)
    remote: dict[str, str] = {}
    for it in items:
        remote.setdefault(it.video_id, it.item_id)
    existing = {
        r["video_id"]: r for r in conn.execute("SELECT * FROM playlist_items WHERE playlist_id=?", (playlist_id,))
    }
    ts = now_iso()
    new_remote = went_missing = 0
    with conn:
        for vid, item_id in remote.items():
            row = existing.get(vid)
            if row is not None:
                conn.execute(
                    """UPDATE playlist_items SET playlist_item_id=COALESCE(playlist_item_id, ?), present=1,
                           last_seen_at=? WHERE playlist_id=? AND video_id=?""",
                    (item_id, ts, playlist_id, vid),
                )
                continue
            linked = conn.execute(
                "SELECT key FROM beatmapsets WHERE chosen_video_id=? ORDER BY date_added LIMIT 1", (vid,)
            ).fetchone()
            conn.execute(
                """INSERT INTO playlist_items(playlist_id, video_id, playlist_item_id, set_key, origin, added_at,
                       present, last_seen_at) VALUES(?, ?, ?, ?, 'remote', NULL, 1, ?)""",
                (playlist_id, vid, item_id, linked["key"] if linked else None, ts),
            )
            new_remote += 1
        for vid, row in existing.items():
            if vid not in remote and row["present"]:
                conn.execute("UPDATE playlist_items SET present=0 WHERE playlist_id=? AND video_id=?",
                             (playlist_id, vid))
                went_missing += 1
        conn.execute("INSERT OR IGNORE INTO playlists(id, title) VALUES(?, ?)", (playlist_id, playlist_id))
        conn.execute("UPDATE playlists SET item_count=?, last_pulled_at=? WHERE id=?", (len(items), ts, playlist_id))
    return PullResult(len(items), new_remote, went_missing, len(items) - len(remote))


@dataclass
class CheckResult:
    checked: int
    unavailable: dict[str, str]   # video_id -> availability
    sets_flagged: int


def check_chosen_videos(conn: sqlite3.Connection, client: YouTubeClient, region: str = "JP",
                        max_age_days: int = 7, force: bool = False) -> CheckResult:
    """videos.list on the chosen videos; sets whose video became unplayable go back to review."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age_days)).isoformat(timespec="seconds")
    rows = conn.execute(
        """SELECT DISTINCT b.chosen_video_id AS vid FROM beatmapsets b
           LEFT JOIN video_checks v ON v.video_id = b.chosen_video_id
           WHERE b.in_library=1 AND b.match_status IN ('confirmed', 'needs_review') AND b.chosen_video_id IS NOT NULL
             AND (? OR v.checked_at IS NULL OR v.checked_at < ?)""",
        (int(force), cutoff),
    ).fetchall()
    ids = [r["vid"] for r in rows]
    if not ids:
        return CheckResult(0, {}, 0)
    result = client.check_videos(ids, region)
    bad, flagged = apply_checks(conn, result)
    return CheckResult(len(ids), bad, flagged)


def apply_checks(conn: sqlite3.Connection, result: dict[str, str]) -> tuple[dict[str, str], int]:
    """Store availability results; sets whose chosen video is unplayable go back to review.

    Returns ({video_id: availability} of the unplayable ones, number of sets sent back to review).
    """
    ts = now_iso()
    bad = {vid: a for vid, a in result.items() if a not in PLAYABLE}
    flagged = 0
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO video_checks(video_id, availability, checked_at) VALUES(?, ?, ?)",
            [(vid, a, ts) for vid, a in result.items()],
        )
        for vid, a in bad.items():
            cur = conn.execute(
                """UPDATE beatmapsets SET match_status='needs_review', review_reason=?, chosen_by='auto', updated_at=?
                   WHERE chosen_video_id=? AND match_status IN ('confirmed', 'needs_review')""",
                (f"unavailable:{a}", ts, vid),
            )
            flagged += cur.rowcount
    return bad, flagged
