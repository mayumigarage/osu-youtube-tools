"""Library listing, song detail and manual match operations."""

from __future__ import annotations

import json
import sqlite3
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..matching import matcher
from ..youtube import playlists
from .deps import bad_request, get_conn

router = APIRouter(prefix="/api/library")

View = Literal["all", "unmatched", "needs_review", "confirmed", "added", "skipped", "gone"]
Sort = Literal["added_desc", "added_asc", "title", "artist", "score_asc", "score_desc", "duration"]

# Display state: gone > added > match_status
_STATE_SQL = """
CASE WHEN b.in_library = 0 THEN 'gone'
     WHEN pi.video_id IS NOT NULL AND b.match_status = 'confirmed' THEN 'added'
     ELSE b.match_status END
"""

_FROM_SQL = """
FROM beatmapsets b
LEFT JOIN candidates c ON c.set_key = b.key AND c.video_id = b.chosen_video_id
LEFT JOIN playlist_items pi ON pi.playlist_id = :pid AND pi.video_id = b.chosen_video_id AND pi.present = 1
LEFT JOIN video_checks vc ON vc.video_id = b.chosen_video_id
"""

_SORTS = {
    "added_desc": "b.date_added DESC",
    "added_asc": "b.date_added ASC",
    "title": "b.title_unicode COLLATE NOCASE",
    "artist": "b.artist_unicode COLLATE NOCASE, b.title_unicode COLLATE NOCASE",
    "score_asc": "COALESCE(b.match_score, -1) ASC, b.date_added DESC",
    "score_desc": "COALESCE(b.match_score, -1) DESC, b.date_added DESC",
    "duration": "b.duration_sec DESC",
}


def _active_pid(conn: sqlite3.Connection) -> str | None:
    row = playlists.active(conn)
    return row["id"] if row else None


def _where(view: str, q: str | None, pack: bool, reason: str | None) -> tuple[str, dict]:
    clauses, params = [], {}
    if view != "all":
        clauses.append(f"({_STATE_SQL}) = :view")
        params["view"] = view
    if q:
        clauses.append("""(b.title LIKE :q OR b.title_unicode LIKE :q OR b.artist LIKE :q OR b.artist_unicode LIKE :q
                            OR b.source LIKE :q OR b.tags LIKE :q OR b.key = :qraw OR c.title LIKE :q)""")
        params["q"], params["qraw"] = f"%{q}%", q
    if pack:
        clauses.append("b.is_pack = 1")
    if reason:
        clauses.append("b.review_reason LIKE :reason")
        params["reason"] = f"{reason}%"
    return ("WHERE " + " AND ".join(clauses)) if clauses else "", params


def _row_to_item(r: sqlite3.Row) -> dict:
    return {
        "key": r["key"], "online_id": r["online_id"],
        "title": r["title"], "title_unicode": r["title_unicode"],
        "artist": r["artist"], "artist_unicode": r["artist_unicode"], "source": r["source"],
        "duration_sec": r["duration_sec"], "duration_source": r["duration_source"],
        "is_pack": bool(r["is_pack"]), "in_library": bool(r["in_library"]),
        "date_added": r["date_added"], "state": r["state"], "match_status": r["match_status"],
        "review_reason": r["review_reason"], "chosen_by": r["chosen_by"], "match_score": r["match_score"],
        "video": None if not r["chosen_video_id"] else {
            "id": r["chosen_video_id"], "title": r["v_title"], "channel": r["v_channel"],
            "duration_sec": r["v_duration"], "thumbnail_url": r["v_thumb"],
            "channel_verified": bool(r["v_verified"]), "availability": r["v_availability"],
        },
    }


_SELECT_SQL = f"""
SELECT b.*, ({_STATE_SQL}) AS state,
       c.title AS v_title, c.channel AS v_channel, c.duration_sec AS v_duration,
       c.thumbnail_url AS v_thumb, c.channel_verified AS v_verified, vc.availability AS v_availability
{_FROM_SQL}
"""


@router.get("")
def list_library(
    view: View = "all", q: str | None = None, pack: bool = False, reason: str | None = None,
    sort: Sort = "added_desc", page: int = Query(1, ge=1), per_page: int = Query(50, ge=1, le=100_000),  # large = "show all" in the UI
    conn: sqlite3.Connection = Depends(get_conn),
):
    pid = _active_pid(conn)
    where, params = _where(view, q, pack, reason)
    params["pid"] = pid
    total = conn.execute(f"SELECT COUNT(*) {_FROM_SQL} {where}", params).fetchone()[0]
    rows = conn.execute(
        f"{_SELECT_SQL} {where} ORDER BY {_SORTS[sort]} LIMIT :limit OFFSET :offset",
        params | {"limit": per_page, "offset": (page - 1) * per_page},
    ).fetchall()
    counts = dict(conn.execute(
        f"SELECT ({_STATE_SQL}) AS state, COUNT(*) {_FROM_SQL} GROUP BY 1", {"pid": pid}).fetchall())
    counts["all"] = sum(counts.values())
    reasons = dict(conn.execute(
        """SELECT CASE WHEN review_reason LIKE 'unavailable:%' THEN 'unavailable' ELSE review_reason END, COUNT(*)
           FROM beatmapsets WHERE in_library=1 AND match_status='needs_review' GROUP BY 1""").fetchall())
    return {"total": total, "page": page, "per_page": per_page, "counts": counts, "reasons": reasons,
            "playlist_id": pid, "items": [_row_to_item(r) for r in rows]}


@router.get("/keys")
def list_keys(view: View = "all", q: str | None = None, pack: bool = False, reason: str | None = None,
              sort: Sort = "added_desc", conn: sqlite3.Connection = Depends(get_conn)):
    """All keys for a filter, in order: used for prev/next navigation in the detail view."""
    where, params = _where(view, q, pack, reason)
    params["pid"] = _active_pid(conn)
    rows = conn.execute(f"SELECT b.key {_FROM_SQL} {where} ORDER BY {_SORTS[sort]}", params).fetchall()
    return [r[0] for r in rows]


def _detail(conn: sqlite3.Connection, key: str) -> dict:
    params = {"pid": _active_pid(conn), "key": key}
    r = conn.execute(f"{_SELECT_SQL} WHERE b.key = :key", params).fetchone()
    if r is None:
        raise HTTPException(404, f"曲が見つかりません: {key}")
    item = _row_to_item(r)
    item["tags"] = r["tags"]
    item["variants"] = json.loads(r["variants_json"] or "[]")
    item["matched_at"] = r["matched_at"]
    cands = conn.execute(
        """SELECT c.*, vc.availability FROM candidates c LEFT JOIN video_checks vc ON vc.video_id = c.video_id
           WHERE c.set_key = ? ORDER BY c.score DESC, c.rank""", (key,)).fetchall()
    item["candidates"] = []
    for c in cands:
        detail = json.loads(c["score_detail"] or "{}")
        item["candidates"].append({
            "video_id": c["video_id"], "title": c["title"], "channel": c["channel"],
            "channel_verified": bool(c["channel_verified"]), "duration_sec": c["duration_sec"],
            "thumbnail_url": c["thumbnail_url"], "score": c["score"], "parts": detail.get("parts", {}),
            "notes": detail.get("notes", []), "query": c["query"], "manual": c["query"] == "manual",
            "availability": c["availability"], "chosen": c["video_id"] == r["chosen_video_id"],
        })
    item["same_video_sets"] = [
        dict(x) for x in conn.execute(
            """SELECT key, artist_unicode, title_unicode FROM beatmapsets
               WHERE chosen_video_id = ? AND key != ? AND in_library = 1""", (r["chosen_video_id"], key))
    ] if r["chosen_video_id"] else []
    return item


@router.get("/{key}")
def get_detail(key: str, conn: sqlite3.Connection = Depends(get_conn)):
    return _detail(conn, key)


class ChooseRequest(BaseModel):
    video: str  # ID or URL


@router.post("/{key}/choose")
def choose(key: str, body: ChooseRequest, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        matcher.choose(conn, key, body.video)
    except matcher.MatchActionError as e:
        raise bad_request(e) from e
    return _detail(conn, key)


@router.post("/{key}/confirm")
def confirm(key: str, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        matcher.confirm(conn, key)
    except matcher.MatchActionError as e:
        raise bad_request(e) from e
    return _detail(conn, key)


@router.post("/{key}/skip")
def skip(key: str, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        matcher.skip(conn, key)
    except matcher.MatchActionError as e:
        raise bad_request(e) from e
    return _detail(conn, key)


@router.post("/{key}/unskip")
def unskip(key: str, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        matcher.unskip(conn, key)
    except matcher.MatchActionError as e:
        raise bad_request(e) from e
    return _detail(conn, key)


@router.post("/{key}/rematch")
def rematch(key: str, conn: sqlite3.Connection = Depends(get_conn)):
    """Search again for one song (a few seconds; runs inline)."""
    row = conn.execute("SELECT * FROM beatmapsets WHERE key=?", (key,)).fetchone()
    if row is None:
        raise HTTPException(404, f"曲が見つかりません: {key}")
    outcome = matcher.match_song(key, matcher.song_from_row(row), bool(row["is_pack"]),
                                 exclude=frozenset(matcher.unplayable_ids(conn)))
    if outcome.error:
        raise HTTPException(502, f"検索に失敗しました: {outcome.error}")
    matcher.save_outcome(conn, outcome)
    return _detail(conn, key)
