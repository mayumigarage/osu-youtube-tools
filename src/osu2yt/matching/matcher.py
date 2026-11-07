"""Search YouTube for each beatmap set, score candidates, auto-accept or flag for review."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable

from ..db import now_iso
from ..library.normalize import (
    clean,
    compact,
    has_japanese,
    looks_romaji,
    romaji_to_katakana,
    strip_brackets,
    strip_version_tags,
)
from . import search as search_mod
from .scoring import Candidate, Decision, Score, Song, decide, score
from .search import SearchError, VideoInfo, parse_video_id

log = logging.getLogger(__name__)

PER_QUERY = 8       # one results page either way; more room to skip chart / play videos
MAX_CANDIDATES = 10

SearchFn = Callable[[str, int], list[VideoInfo]]
FetchFn = Callable[[str], VideoInfo | None]


@dataclass
class Scored:
    info: VideoInfo
    score: Score
    query: str
    rank: int


@dataclass
class MatchOutcome:
    key: str
    queries: list[str] = field(default_factory=list)
    candidates: list[Scored] = field(default_factory=list)
    decision: Decision | None = None
    error: str | None = None


def song_from_row(row) -> Song:
    keys = row.keys()
    return Song(
        title=row["title"] or "", title_unicode=row["title_unicode"] or "",
        artist=row["artist"] or "", artist_unicode=row["artist_unicode"] or "",
        duration_sec=row["duration_sec"],
        source=(row["source"] if "source" in keys else "") or "",
        tags=(row["tags"] if "tags" in keys else "") or "",
    )


# YouTube reads these as search operators: "-word" excludes, "quotes" force a phrase, "#tag" ...
# Titles like "Hoshizora no kanran -sha-" or "lilac slumber -cosmic ver.-" otherwise exclude
# exactly the videos we want.
_EDGE_DASH_RE = re.compile(r"(?<!\w)[-‐‑‒–—―~～〜]+|[-‐‑‒–—―~～〜]+(?!\w)")
_OPERATOR_RE = re.compile(r"[\"“”„＂|｜]|(?<!\S)[#＃+＋@＠]+")
_PARTICLES = {"no", "wo", "wa", "ni", "ga", "to", "de", "mo", "e", "he", "ya"}


def youtube_query(s: str) -> str:
    """Neutralise characters YouTube treats as operators; keep everything else as typed."""
    return clean(_OPERATOR_RE.sub(" ", _EDGE_DASH_RE.sub(" ", s or "")))


def build_queries(song: Song) -> list[str]:
    """Unicode 'Artist Title' first, then romanised, then title alone. Short-version tags removed.

    A romaji-only title ('Arumagedon' with no Japanese metadata) also gets a katakana query last:
    YouTube "corrects" such romaji to unrelated English words but finds 'アルマゲドン' / 'あるまげどん'.
    """
    def artist_for_query(a: str) -> str:
        return strip_brackets(a) or clean(a)  # drop '(CV: ...)' noise

    raw = [
        f"{artist_for_query(song.artist_unicode)} {strip_version_tags(song.title_unicode)}",
        f"{artist_for_query(song.artist)} {strip_version_tags(song.title)}",
        strip_version_tags(song.title_unicode),
    ]
    title = strip_brackets(strip_version_tags(song.title))
    if not has_japanese(song.title_unicode) and looks_romaji(title):
        raw.append(f"{artist_for_query(song.artist_unicode)} {romaji_to_katakana(title)}")
    out, seen = [], set()
    for q in raw:
        q = youtube_query(q)
        k = compact(q)
        if k and k not in seen:
            seen.add(k)
            out.append(q)
    return out


def zero_result_fallbacks(query: str) -> list[str]:
    """Alternatives for a query YouTube returns nothing for.

    YouTube answers some romaji phrases with zero results ('Hatsune Miku no Shoushitsu',
    'Nou Shou Sakuretsu Girl') while the same words plus 'song', or without the particles, work.
    """
    out = [f"{query} song"]
    words = query.split()
    no_particles = " ".join(w for w in words if w.lower() not in _PARTICLES)
    if no_particles and no_particles != query:
        out.append(no_particles)
    return out


def _to_candidate(info: VideoInfo) -> Candidate:
    return Candidate(info.video_id, info.title, info.channel, info.channel_verified, info.duration_sec, info.live)


def unplayable_ids(conn: sqlite3.Connection) -> set[str]:
    """Videos known (via videos.list) to be deleted / private / region blocked."""
    return {r[0] for r in conn.execute(
        "SELECT video_id FROM video_checks WHERE availability NOT IN ('ok', 'not_embeddable')")}


def match_song(
    key: str, song: Song, is_pack: bool = False, search_fn: SearchFn | None = None,
    cancel: threading.Event | None = None, exclude: set[str] | frozenset[str] = frozenset(),
) -> MatchOutcome:
    """Run queries until a confident match is found (or queries run out). No DB access."""
    search_fn = search_fn or search_mod.search
    outcome = MatchOutcome(key=key)
    pool: dict[str, Scored] = {}
    try:
        for q in build_queries(song):
            if cancel and cancel.is_set():
                outcome.error = "cancelled"
                return outcome
            outcome.queries.append(q)
            results = search_fn(q, PER_QUERY)
            if not results:
                for alt in zero_result_fallbacks(q):
                    outcome.queries.append(alt)
                    results = search_fn(alt, PER_QUERY)
                    if results:
                        q = alt
                        break
            for rank, info in enumerate(results, start=1):
                if info.video_id not in pool and info.video_id not in exclude:
                    pool[info.video_id] = Scored(info, score(song, _to_candidate(info)), q, rank)
            ranked = sorted(pool.values(), key=lambda s: (-s.score.total, s.rank))
            outcome.decision = decide([(s.info.video_id, s.score, s.info.duration_sec) for s in ranked], is_pack)
            if outcome.decision.status == "confirmed":
                break
    except SearchError as e:
        outcome.error = str(e)
        return outcome
    outcome.candidates = sorted(pool.values(), key=lambda s: (-s.score.total, s.rank))[:MAX_CANDIDATES]
    if outcome.decision is None:
        outcome.decision = decide([], is_pack)
    return outcome


def _insert_candidate(conn: sqlite3.Connection, key: str, s: Scored, rank: int, ts: str) -> None:
    detail = json.dumps({"parts": s.score.parts, "notes": s.score.notes}, ensure_ascii=False)
    conn.execute(
        """INSERT OR REPLACE INTO candidates(set_key, video_id, rank, query, title, channel, channel_id,
               channel_verified, duration_sec, thumbnail_url, score, score_detail, fetched_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (key, s.info.video_id, rank, s.query, s.info.title, s.info.channel, s.info.channel_id,
         int(s.info.channel_verified), s.info.duration_sec, s.info.thumbnail_url, s.score.total, detail, ts),
    )


def save_outcome(conn: sqlite3.Connection, outcome: MatchOutcome) -> None:
    if outcome.error:
        return  # leave the set as it was; it will be retried next time
    ts = now_iso()
    d = outcome.decision
    prev = conn.execute("SELECT chosen_by, chosen_video_id FROM beatmapsets WHERE key=?", (outcome.key,)).fetchone()
    keep_choice = prev is not None and prev["chosen_by"] in ("user", "manual")
    with conn:
        # keep manual candidates and whatever the user picked; refresh the rest
        conn.execute(
            "DELETE FROM candidates WHERE set_key=? AND query != 'manual' AND video_id IS NOT ?",
            (outcome.key, prev["chosen_video_id"] if keep_choice else None),
        )
        for i, s in enumerate(outcome.candidates, start=1):
            _insert_candidate(conn, outcome.key, s, i, ts)
        if keep_choice:
            conn.execute("UPDATE beatmapsets SET matched_at=?, updated_at=? WHERE key=?", (ts, ts, outcome.key))
            return
        conn.execute(
            """UPDATE beatmapsets SET match_status=?, chosen_video_id=?, chosen_by=?, match_score=?,
                   review_reason=?, matched_at=?, updated_at=? WHERE key=?""",
            (d.status, d.video_id, "auto" if d.video_id else None, d.score, d.reason, ts, ts, outcome.key),
        )


def select_rows(conn: sqlite3.Connection, scope: str = "unmatched", keys: list[str] | None = None,
                limit: int | None = None) -> list[sqlite3.Row]:
    if scope == "keys":
        marks = ",".join("?" * len(keys or []))
        rows = conn.execute(f"SELECT * FROM beatmapsets WHERE key IN ({marks})", keys or []).fetchall()
    elif scope == "unmatched":
        rows = conn.execute(
            "SELECT * FROM beatmapsets WHERE in_library=1 AND match_status='unmatched' ORDER BY date_added DESC"
        ).fetchall()
    elif scope == "needs_review":
        # never overwrite a choice the user made
        rows = conn.execute(
            """SELECT * FROM beatmapsets WHERE in_library=1 AND match_status='needs_review'
                 AND COALESCE(chosen_by, 'auto') = 'auto' ORDER BY date_added DESC"""
        ).fetchall()
    elif scope == "auto":
        # everything decided automatically (e.g. after tuning the scoring); user choices untouched
        rows = conn.execute(
            """SELECT * FROM beatmapsets WHERE in_library=1 AND match_status IN ('confirmed', 'needs_review')
                 AND COALESCE(chosen_by, 'auto') = 'auto' ORDER BY date_added DESC"""
        ).fetchall()
    else:
        raise ValueError(f"unknown scope {scope}")
    return rows[:limit] if limit else rows


@dataclass
class RunSummary:
    total: int = 0
    confirmed: int = 0
    needs_review: int = 0
    failed: int = 0
    cancelled: bool = False


def run_matching(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
    workers: int = 3,
    search_fn: SearchFn | None = None,
    progress: Callable[[int, int, MatchOutcome], None] | None = None,
    cancel: threading.Event | None = None,
) -> RunSummary:
    """Searches run in worker threads; all DB writes happen on the calling thread."""
    summary = RunSummary(total=len(rows))
    exclude = frozenset(unplayable_ids(conn))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(match_song, r["key"], song_from_row(r), bool(r["is_pack"]), search_fn, cancel, exclude)
            for r in rows
        ]
        for done, fut in enumerate(as_completed(futures), start=1):
            outcome = fut.result()
            if outcome.error == "cancelled":
                summary.cancelled = True
            elif outcome.error:
                summary.failed += 1
                log.warning("match failed for %s: %s", outcome.key, outcome.error)
            else:
                save_outcome(conn, outcome)
                if outcome.decision.status == "confirmed":
                    summary.confirmed += 1
                else:
                    summary.needs_review += 1
            if progress:
                progress(done, len(rows), outcome)
    return summary


def rescore(conn: sqlite3.Connection, rows: list[sqlite3.Row]) -> RunSummary:
    """Recompute scores/decisions from stored candidates without searching again (after tuning)."""
    summary = RunSummary(total=len(rows))
    exclude = unplayable_ids(conn)
    for r in rows:
        song = song_from_row(r)
        scored = []
        for c in conn.execute("SELECT * FROM candidates WHERE set_key=? AND query != 'manual'", (r["key"],)):
            if c["video_id"] in exclude:
                continue
            info = VideoInfo(c["video_id"], c["title"] or "", c["channel"] or "", c["channel_id"],
                             bool(c["channel_verified"]), c["duration_sec"])
            scored.append(Scored(info, score(song, _to_candidate(info)), c["query"], c["rank"] or 99))
        scored.sort(key=lambda s: (-s.score.total, s.rank))
        outcome = MatchOutcome(
            key=r["key"], candidates=scored,
            decision=decide([(s.info.video_id, s.score, s.info.duration_sec) for s in scored], bool(r["is_pack"])),
        )
        save_outcome(conn, outcome)
        if outcome.decision.status == "confirmed":
            summary.confirmed += 1
        else:
            summary.needs_review += 1
    return summary


# --- manual operations (shared by CLI and WebUI) --------------------------------------------------

class MatchActionError(ValueError):
    pass


def _get_set(conn: sqlite3.Connection, key: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM beatmapsets WHERE key=?", (key,)).fetchone()
    if not row:
        raise MatchActionError(f"曲が見つかりません: {key}")
    return row


def choose(conn: sqlite3.Connection, key: str, video: str, fetch_fn: FetchFn | None = None) -> str:
    """Adopt a candidate, or any video given as ID/URL. Returns 'user' or 'manual'."""
    fetch_fn = fetch_fn or search_mod.fetch_video
    row = _get_set(conn, key)
    vid = parse_video_id(video)
    if not vid:
        raise MatchActionError(f"動画IDまたはURLとして解釈できません: {video}")
    ts = now_iso()
    cand = conn.execute("SELECT score FROM candidates WHERE set_key=? AND video_id=?", (key, vid)).fetchone()
    if cand:
        chosen_by, sc = "user", cand["score"]
    else:
        info = fetch_fn(vid)
        if info is None:
            raise MatchActionError(f"動画を取得できません (削除/非公開の可能性): {vid}")
        s = score(song_from_row(row), _to_candidate(info))
        with conn:
            _insert_candidate(conn, key, Scored(info, s, "manual", 0), 0, ts)
        chosen_by, sc = "manual", s.total
    with conn:
        conn.execute(
            """UPDATE beatmapsets SET match_status='confirmed', chosen_video_id=?, chosen_by=?, match_score=?,
                   review_reason=NULL, matched_at=?, updated_at=? WHERE key=?""",
            (vid, chosen_by, sc, ts, ts, key),
        )
    return chosen_by


def skip(conn: sqlite3.Connection, key: str) -> None:
    _get_set(conn, key)
    with conn:
        conn.execute("UPDATE beatmapsets SET match_status='skipped', updated_at=? WHERE key=?", (now_iso(), key))


def unskip(conn: sqlite3.Connection, key: str) -> str:
    row = _get_set(conn, key)
    if row["chosen_video_id"] and row["chosen_by"] in ("user", "manual"):
        status = "confirmed"
    elif row["chosen_video_id"]:
        status = "needs_review"
    else:
        status = "unmatched"
    with conn:
        conn.execute("UPDATE beatmapsets SET match_status=?, updated_at=? WHERE key=?", (status, now_iso(), key))
    return status


def confirm(conn: sqlite3.Connection, key: str) -> None:
    """Accept the currently chosen (auto) candidate of a needs_review set as is."""
    row = _get_set(conn, key)
    if not row["chosen_video_id"]:
        raise MatchActionError("採用中の動画がありません")
    with conn:
        conn.execute(
            """UPDATE beatmapsets SET match_status='confirmed', chosen_by='user', review_reason=NULL,
                   updated_at=? WHERE key=?""",
            (now_iso(), key),
        )

