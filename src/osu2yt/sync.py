"""Diff sync: add the chosen videos of confirmed songs to the active playlist.

Rules
- One playlist entry per video, even when several beatmap sets chose the same video.
- Anything already in the playlist is skipped (the playlist is pulled before adding).
- Videos the user removed from the playlist on YouTube (present=0) are never re-added.
- Nothing is ever deleted from the playlist.
- quotaExceeded (Google's or our own budget) pauses the run safely; the next run simply picks up
  whatever is still missing, so resuming is "run again" (done automatically after the PT reset).
"""

from __future__ import annotations

import logging
import math
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .db import now_iso
from .logging_setup import mask
from .youtube import playlists, quota
from .youtube.client import PLAYABLE, NotFound, QuotaExceeded, YouTubeClient, YouTubeError

log = logging.getLogger(__name__)

INSERT_UNITS = quota.COSTS["playlistItems.insert"]
RECHECK_DAYS = 7                 # re-run videos.list on videos checked longer ago than this
MAX_CONSECUTIVE_FAILURES = 5     # something is wrong with the playlist/account rather than the videos

Progress = Callable[[str, int, int], None]   # (stage, done, total)


class SyncError(RuntimeError):
    pass


# --- planning (local only, no API calls) ---------------------------------------------------------

@dataclass
class PlanItem:
    video_id: str
    set_key: str                 # representative set (oldest), recorded on the playlist item
    set_keys: list[str]
    title: str
    artist: str
    availability: str | None = None
    checked_at: str | None = None


@dataclass
class Plan:
    playlist_id: str | None
    to_add: list[PlanItem] = field(default_factory=list)
    confirmed_sets: int = 0
    unique_videos: int = 0
    already_present: int = 0     # unique videos already in the playlist
    removed_remote: int = 0      # removed on YouTube by the user: not re-added
    unavailable: int = 0         # known to be deleted/private/region-blocked
    needs_review: int = 0
    pulled_at: str | None = None


def plan(conn: sqlite3.Connection, playlist_id: str | None) -> Plan:
    p = Plan(playlist_id)
    p.needs_review = conn.execute(
        "SELECT COUNT(*) FROM beatmapsets WHERE in_library=1 AND match_status='needs_review'").fetchone()[0]
    rows = conn.execute(
        """SELECT key, chosen_video_id AS vid, title_unicode, artist_unicode FROM beatmapsets
           WHERE in_library=1 AND match_status='confirmed' AND chosen_video_id IS NOT NULL
           ORDER BY date_added, key"""
    ).fetchall()
    p.confirmed_sets = len(rows)
    groups: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        groups.setdefault(r["vid"], []).append(r)
    p.unique_videos = len(groups)

    present: dict[str, int] = {}
    if playlist_id:
        present = {r["video_id"]: r["present"] for r in conn.execute(
            "SELECT video_id, present FROM playlist_items WHERE playlist_id=?", (playlist_id,))}
        row = conn.execute("SELECT last_pulled_at FROM playlists WHERE id=?", (playlist_id,)).fetchone()
        p.pulled_at = row["last_pulled_at"] if row else None
    checks = {r["video_id"]: (r["availability"], r["checked_at"]) for r in conn.execute(
        "SELECT video_id, availability, checked_at FROM video_checks")}

    for vid, sets in groups.items():
        if vid in present:
            if present[vid]:
                p.already_present += 1
            else:
                p.removed_remote += 1
            continue
        avail, checked_at = checks.get(vid, (None, None))
        if avail and avail not in PLAYABLE:
            p.unavailable += 1
            continue
        first = sets[0]
        p.to_add.append(PlanItem(vid, first["key"], [s["key"] for s in sets], first["title_unicode"],
                                 first["artist_unicode"], avail, checked_at))
    return p


def _stale(item: PlanItem, now: datetime) -> bool:
    if not item.checked_at:
        return True
    try:
        return datetime.fromisoformat(item.checked_at) < now - timedelta(days=RECHECK_DAYS)
    except ValueError:
        return True


def estimate(conn: sqlite3.Connection, p: Plan, max_add: int | None = None) -> dict:
    """Quota estimate for syncing the plan (pull + availability check + inserts)."""
    n = len(p.to_add) if max_add is None else min(len(p.to_add), max_add)
    item_count = 0
    if p.playlist_id:
        row = conn.execute("SELECT item_count FROM playlists WHERE id=?", (p.playlist_id,)).fetchone()
        item_count = (row["item_count"] if row else None) or 0
    now = datetime.now(timezone.utc)
    pull = max(1, math.ceil(item_count / 50))
    to_check = sum(1 for it in p.to_add[:n] if _stale(it, now))
    check = math.ceil(to_check / 50)
    inserts = n * INSERT_UNITS
    st = quota.status(conn)
    today = min(n, max(0, st.available - pull - check) // INSERT_UNITS)
    per_day = max(1, (st.limit - st.margin - pull) // INSERT_UNITS)
    rest = n - today
    days = (1 if today else 0) + math.ceil(rest / per_day)
    return {
        "count": n, "pull_units": pull, "check_units": check, "insert_units": inserts,
        "total_units": pull + check + inserts, "inserts_today": today, "per_day": per_day,
        "days": days, "available_today": st.available, "reset_at": st.reset_at.isoformat(),
    }


# --- running ---------------------------------------------------------------------------------------

@dataclass
class SyncResult:
    run_id: int
    status: str                  # done / paused_quota / cancelled / error
    dry_run: bool
    planned: int = 0
    added: int = 0
    skipped_dup: int = 0
    unavailable: int = 0
    failed: int = 0
    remaining: int = 0
    quota_used: int = 0
    error: str | None = None
    pull: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def run_sync(
    conn: sqlite3.Connection,
    client: YouTubeClient,
    playlist_id: str,
    *,
    dry_run: bool = False,
    max_add: int | None = None,
    region: str = "JP",
    trigger: str = "manual",
    progress: Progress | None = None,
    cancel: threading.Event | None = None,
    note: Callable[[str], None] | None = None,
) -> SyncResult:
    progress = progress or (lambda *a: None)
    note = note or (lambda s: log.info("%s", s))
    started = now_iso()
    with conn:
        run_id = conn.execute(
            """INSERT INTO sync_runs(playlist_id, started_at, status, dry_run, trigger, pt_date,
                   planned, added, skipped_dup, failed, unavailable, quota_used)
               VALUES(?, ?, 'running', ?, ?, ?, 0, 0, 0, 0, 0, 0)""",
            (playlist_id, started, int(dry_run), trigger, quota.pt_date()),
        ).lastrowid
        if not dry_run:  # an earlier paused run's leftovers are re-planned by this run
            conn.execute("UPDATE sync_queue SET state='carried_over' WHERE state='pending' AND sync_run_id<>?", (run_id,))
    res = SyncResult(run_id, "running", dry_run)
    try:
        _run(conn, client, playlist_id, res, dry_run, max_add, region, progress, cancel, note)
    except QuotaExceeded as e:
        res.status, res.error = "paused_quota", str(e)
        note(f"クォータ上限で一時停止: {e}")
    except Exception as e:  # noqa: BLE001 - recorded in history, re-raised for the job runner
        res.status, res.error = "error", mask(str(e))
        _finish(conn, res, started)
        raise
    _finish(conn, res, started)
    return res


def _run(conn, client, playlist_id, res: SyncResult, dry_run, max_add, region, progress, cancel, note):
    progress("pull", 0, 0)
    try:
        pr = playlists.pull_items(conn, client, playlist_id)
    except NotFound as e:
        raise SyncError("プレイリストが見つかりません (YouTube 上で削除された可能性があります)。設定で選び直してください。") from e
    res.pull = asdict(pr)
    note(f"プレイリストの現在の中身: {pr.total} 件 (新たに見つかった {pr.new_remote} / 消えていた {pr.went_missing})")

    p = plan(conn, playlist_id)
    items = p.to_add if max_add is None else p.to_add[:max_add]
    res.planned = len(items)
    ts = now_iso()
    with conn:
        conn.executemany(
            """INSERT INTO sync_queue(sync_run_id, set_key, video_id, state, attempts, position, updated_at)
               VALUES(?, ?, ?, 'pending', 0, ?, ?)""",
            [(res.run_id, it.set_key, it.video_id, i, ts) for i, it in enumerate(items)],
        )
        conn.execute("UPDATE sync_runs SET planned=? WHERE id=?", (res.planned, res.run_id))
    note(f"追加予定 {len(items)} 本 (確定 {p.confirmed_sets} 譜面 / 動画 {p.unique_videos} 本 / "
         f"追加済み {p.already_present} / YouTube 上で削除済み {p.removed_remote} は再追加しません)")
    if not items:
        res.status = "done"
        return

    # availability: skip videos that became deleted/private/region-blocked since matching
    now = datetime.now(timezone.utc)
    stale = [it.video_id for it in items if _stale(it, now)]
    if stale:
        progress("check", 0, len(stale))
        bad, flagged = playlists.apply_checks(conn, client.check_videos(stale, region))
        if bad:
            with conn:
                conn.executemany(
                    "UPDATE sync_queue SET state='unavailable', last_error=?, updated_at=? WHERE sync_run_id=? AND video_id=?",
                    [(a, now_iso(), res.run_id, vid) for vid, a in bad.items()],
                )
            res.unavailable = len(bad)
            note(f"再生できない動画 {len(bad)} 本を除外 ({flagged} 譜面を要確認に戻しました)")

    pending = [r["video_id"] for r in conn.execute(
        "SELECT video_id FROM sync_queue WHERE sync_run_id=? AND state='pending' ORDER BY position", (res.run_id,))]
    if dry_run:
        with conn:
            conn.execute("UPDATE sync_queue SET state='planned' WHERE sync_run_id=? AND state='pending'", (res.run_id,))
        res.status = "done"
        note(f"ドライラン: {len(pending)} 本を追加する予定です (追加はしていません)")
        return

    by_vid = {it.video_id: it for it in items}
    consecutive = 0
    for i, vid in enumerate(pending):
        if cancel and cancel.is_set():
            res.status = "cancelled"
            note("中断しました")
            return
        progress("insert", i, len(pending))
        if conn.execute("SELECT 1 FROM playlist_items WHERE playlist_id=? AND video_id=? AND present=1",
                        (playlist_id, vid)).fetchone():
            _set_state(conn, res.run_id, vid, "dup")
            res.skipped_dup += 1
            continue
        try:
            item_id = client.insert_playlist_item(playlist_id, vid)
        except QuotaExceeded:
            raise
        except NotFound as e:
            if "playlistNotFound" in str(e):
                raise SyncError("プレイリストが見つかりません (YouTube 上で削除された可能性があります)。") from e
            playlists.apply_checks(conn, {vid: "deleted"})
            _set_state(conn, res.run_id, vid, "unavailable", "deleted")
            res.unavailable += 1
            note(f"動画が見つかりません: {vid} ({by_vid[vid].title})")
            continue
        except YouTubeError as e:
            _set_state(conn, res.run_id, vid, "failed", mask(str(e)))
            res.failed += 1
            consecutive += 1
            note(f"追加に失敗: {vid} ({by_vid[vid].title}): {mask(str(e))}")
            if consecutive >= MAX_CONSECUTIVE_FAILURES:
                raise SyncError(f"{consecutive} 回連続で失敗したため中止しました。最後のエラー: {mask(str(e))}") from e
            continue
        consecutive = 0
        it = by_vid[vid]
        with conn:
            conn.execute(
                """INSERT INTO playlist_items(playlist_id, video_id, playlist_item_id, set_key, origin, added_at,
                       sync_run_id, present, last_seen_at) VALUES(?, ?, ?, ?, 'tool', ?, ?, 1, ?)
                   ON CONFLICT(playlist_id, video_id) DO UPDATE SET playlist_item_id=excluded.playlist_item_id,
                       present=1, added_at=excluded.added_at, sync_run_id=excluded.sync_run_id,
                       last_seen_at=excluded.last_seen_at""",
                (playlist_id, vid, item_id, it.set_key, now_iso(), res.run_id, now_iso()),
            )
            conn.execute("UPDATE playlists SET item_count=COALESCE(item_count, 0)+1 WHERE id=?", (playlist_id,))
            conn.execute("UPDATE sync_queue SET state='added', attempts=attempts+1, updated_at=? WHERE sync_run_id=? AND video_id=?",
                         (now_iso(), res.run_id, vid))
        res.added += 1
    progress("insert", len(pending), len(pending))
    res.status = "done"


def _set_state(conn, run_id: int, vid: str, state: str, error: str | None = None) -> None:
    with conn:
        conn.execute(
            """UPDATE sync_queue SET state=?, last_error=?, attempts=attempts+?, updated_at=?
               WHERE sync_run_id=? AND video_id=?""",
            (state, error, 0 if state == "dup" else 1, now_iso(), run_id, vid),
        )


def _finish(conn: sqlite3.Connection, res: SyncResult, started: str) -> None:
    res.remaining = conn.execute(
        "SELECT COUNT(*) FROM sync_queue WHERE sync_run_id=? AND state='pending'", (res.run_id,)).fetchone()[0]
    res.quota_used = conn.execute(
        "SELECT COALESCE(SUM(units), 0) FROM quota_log WHERE ts >= ?", (started,)).fetchone()[0]
    with conn:
        conn.execute(
            """UPDATE sync_runs SET finished_at=?, status=?, planned=?, added=?, skipped_dup=?, failed=?,
                   unavailable=?, remaining=?, quota_used=?, error=? WHERE id=?""",
            (now_iso(), res.status, res.planned, res.added, res.skipped_dup, res.failed, res.unavailable,
             res.remaining, res.quota_used, res.error, res.run_id),
        )


def mark_interrupted(conn: sqlite3.Connection) -> int:
    """Runs left 'running' by a crash or a stopped server: their leftovers are re-planned next time."""
    with conn:
        return conn.execute(
            "UPDATE sync_runs SET status='interrupted', finished_at=COALESCE(finished_at, ?) WHERE status='running'",
            (now_iso(),),
        ).rowcount


# --- resume ------------------------------------------------------------------------------------------

def resume_due(conn: sqlite3.Connection, playlist_id: str | None, now: datetime | None = None) -> sqlite3.Row | None:
    """The latest real run, if it was paused by quota on an earlier PT day and things remain to add."""
    if not playlist_id:
        return None
    last = conn.execute(
        "SELECT * FROM sync_runs WHERE dry_run=0 AND status<>'running' ORDER BY id DESC LIMIT 1").fetchone()
    if not last or last["status"] != "paused_quota" or last["playlist_id"] != playlist_id:
        return None
    if (last["pt_date"] or "") >= quota.pt_date(now):
        return None
    if not plan(conn, playlist_id).to_add:
        return None
    return last


# --- history ----------------------------------------------------------------------------------------

def runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    rows = conn.execute(
        """SELECT r.*, p.title AS playlist_title FROM sync_runs r LEFT JOIN playlists p ON p.id = r.playlist_id
           ORDER BY r.id DESC LIMIT ?""", (limit,)).fetchall()
    return [dict(r) for r in rows]


def run_detail(conn: sqlite3.Connection, run_id: int) -> dict | None:
    row = conn.execute(
        """SELECT r.*, p.title AS playlist_title FROM sync_runs r LEFT JOIN playlists p ON p.id = r.playlist_id
           WHERE r.id=?""", (run_id,)).fetchone()
    if not row:
        return None
    items = conn.execute(
        """SELECT q.position, q.set_key, q.video_id, q.state, q.last_error, q.updated_at,
                  b.title_unicode, b.artist_unicode, c.title AS video_title
           FROM sync_queue q
           LEFT JOIN beatmapsets b ON b.key = q.set_key
           LEFT JOIN candidates c ON c.set_key = q.set_key AND c.video_id = q.video_id
           WHERE q.sync_run_id=? ORDER BY q.position""", (run_id,)).fetchall()
    return {**dict(row), "items": [dict(i) for i in items]}
