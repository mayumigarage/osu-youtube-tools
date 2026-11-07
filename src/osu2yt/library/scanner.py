"""Library scan: copy realm -> helper export -> diff against SQLite -> audio lengths."""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from ..db import now_iso
from . import audio, helper
from .normalize import clean, is_pack, primary_variant
from .realm_copy import copy_realm, stat_realm

log = logging.getLogger(__name__)

Progress = Callable[[str, int, int], None]  # (stage, done, total)


@dataclass
class SetRecord:
    key: str
    online_id: int | None
    realm_guid: str | None
    title: str
    title_unicode: str
    artist: str
    artist_unicode: str
    source: str
    tags: str
    audio_hash: str | None
    map_length_sec: float | None
    variants: list[dict]
    is_pack: bool
    date_added: str | None
    difficulty_count: int = 0


@dataclass
class DiffResult:
    new: set[str] = field(default_factory=set)
    kept: set[str] = field(default_factory=set)
    gone: set[str] = field(default_factory=set)
    returned: set[str] = field(default_factory=set)


@dataclass
class ScanResult:
    status: str  # done / skipped
    scan_id: int | None = None
    total: int = 0
    new: int = 0
    gone: int = 0
    returned: int = 0
    durations_probed: int = 0
    warnings: list[str] = field(default_factory=list)


def set_key(s: dict) -> str | None:
    """'o:<OnlineID>' when the set is on the website, else 'g:<realm GUID>'."""
    online_id = s.get("onlineId") or 0
    if online_id > 0:
        return f"o:{online_id}"
    if s.get("id"):
        return f"g:{s['id']}"
    return None


def build_records(export: dict, include_protected: bool = False) -> dict[str, SetRecord]:
    """Pure: helper JSON -> one record per beatmap set that should be in the library."""
    records: dict[str, SetRecord] = {}
    for s in export.get("sets", []):
        if s.get("deletePending"):
            continue  # treated as "no longer in library"
        if s.get("protected") and not include_protected:
            continue  # lazer's bundled sets (e.g. "triangles")
        key = set_key(s)
        if key is None:
            continue
        variants = s.get("variants") or []
        pv = primary_variant(variants) or {}
        title = clean(pv.get("title"))
        artist = clean(pv.get("artist"))
        length_ms = pv.get("maxLengthMs") or s.get("maxLengthMs") or 0
        rec = SetRecord(
            key=key,
            online_id=s.get("onlineId") if (s.get("onlineId") or 0) > 0 else None,
            realm_guid=s.get("id"),
            title=title,
            title_unicode=clean(pv.get("titleUnicode")) or title,
            artist=artist,
            artist_unicode=clean(pv.get("artistUnicode")) or artist,
            source=clean(pv.get("source")),
            tags=clean(pv.get("tags")),
            audio_hash=pv.get("audioHash"),
            map_length_sec=round(length_ms / 1000, 3) if length_ms > 0 else None,
            variants=variants,
            is_pack=is_pack(variants),
            date_added=s.get("dateAdded"),
            difficulty_count=s.get("difficultyCount") or 0,
        )
        prev = records.get(key)
        # Two local copies of the same online set: keep the one with more difficulties.
        if prev is None or rec.difficulty_count > prev.difficulty_count:
            records[key] = rec
    return records


def diff_keys(existing: dict[str, bool], incoming: Iterable[str]) -> DiffResult:
    """Pure: existing = {key: in_library}; incoming = keys present in this scan."""
    incoming = set(incoming)
    return DiffResult(
        new=incoming - existing.keys(),
        kept=incoming & existing.keys(),
        gone={k for k, in_lib in existing.items() if in_lib and k not in incoming},
        returned={k for k, in_lib in existing.items() if not in_lib and k in incoming},
    )


def _variants_json(variants: list[dict]) -> str:
    slim = [
        {k: v.get(k) for k in ("title", "titleUnicode", "artist", "artistUnicode", "audioFile",
                                "audioHash", "maxLengthMs", "difficultyCount")}
        for v in variants
    ]
    return json.dumps(slim, ensure_ascii=False)


def apply_export(
    conn: sqlite3.Connection, export: dict, scan_id: int, include_protected: bool = False
) -> tuple[DiffResult, dict[str, SetRecord]]:
    """Merge a helper export into beatmapsets. Match results of existing sets are preserved."""
    records = build_records(export, include_protected)
    existing = {r["key"]: bool(r["in_library"]) for r in conn.execute("SELECT key, in_library FROM beatmapsets")}
    diff = diff_keys(existing, records)
    ts = now_iso()
    with conn:
        for key in diff.new:
            r = records[key]
            conn.execute(
                """INSERT INTO beatmapsets(key, online_id, realm_guid, title, title_unicode, artist,
                       artist_unicode, source, tags, audio_hash, map_length_sec, variants_json, is_pack,
                       date_added, first_seen_scan, last_seen_scan, in_library, match_status, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'unmatched',?)""",
                (key, r.online_id, r.realm_guid, r.title, r.title_unicode, r.artist, r.artist_unicode,
                 r.source, r.tags, r.audio_hash, r.map_length_sec, _variants_json(r.variants),
                 int(r.is_pack), r.date_added, scan_id, scan_id, ts),
            )
        for key in diff.kept:
            r = records[key]
            conn.execute(
                """UPDATE beatmapsets SET realm_guid=?, title=?, title_unicode=?, artist=?, artist_unicode=?,
                       source=?, tags=?, map_length_sec=?, variants_json=?, is_pack=?, date_added=?,
                       last_seen_scan=?, in_library=1, updated_at=?,
                       duration_sec = CASE WHEN audio_hash IS ? THEN duration_sec ELSE NULL END,
                       duration_source = CASE WHEN audio_hash IS ? THEN duration_source ELSE NULL END,
                       audio_hash=?
                   WHERE key=?""",
                (r.realm_guid, r.title, r.title_unicode, r.artist, r.artist_unicode, r.source, r.tags,
                 r.map_length_sec, _variants_json(r.variants), int(r.is_pack), r.date_added, scan_id, ts,
                 r.audio_hash, r.audio_hash, r.audio_hash, key),
            )
        for key in diff.gone:
            conn.execute("UPDATE beatmapsets SET in_library=0, updated_at=? WHERE key=?", (ts, key))
    return diff, records


def fill_durations(conn: sqlite3.Connection, lazer_dir: Path, progress: Progress | None = None) -> int:
    """Set duration_sec from the real audio file (ffprobe, cached by hash), else the map length.

    Returns the number of audio files probed.
    """
    rows = conn.execute(
        """SELECT key, audio_hash, map_length_sec FROM beatmapsets
           WHERE duration_source IS NULL
              OR (duration_source != 'audio' AND audio_hash IS NOT NULL
                  AND audio_hash NOT IN (SELECT audio_hash FROM audio_durations))"""
    ).fetchall()
    if not rows:
        return 0
    cached = {r[0]: r[1] for r in conn.execute("SELECT audio_hash, duration_sec FROM audio_durations")}
    to_probe = [r["audio_hash"] for r in rows if r["audio_hash"] and r["audio_hash"] not in cached]
    probed = audio.probe_many(
        lazer_dir, to_probe, progress=(lambda d, t: progress("audio", d, t)) if progress else None
    )
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO audio_durations(audio_hash, duration_sec) VALUES(?, ?)", probed.items()
        )
        cached.update(probed)
        for r in rows:
            d = cached.get(r["audio_hash"]) if r["audio_hash"] else None
            if d:
                value, source = round(d, 3), "audio"
            elif r["map_length_sec"]:
                value, source = r["map_length_sec"], "map"
            else:
                value, source = None, "unknown"
            conn.execute(
                "UPDATE beatmapsets SET duration_sec=?, duration_source=? WHERE key=?", (value, source, r["key"])
            )
    return len(probed)


def scan(
    conn: sqlite3.Connection,
    lazer_dir: Path,
    work_dir: Path,
    force: bool = False,
    include_protected: bool = False,
    progress: Progress | None = None,
) -> ScanResult:
    lazer_dir = Path(lazer_dir)
    current = stat_realm(lazer_dir)
    last = conn.execute(
        "SELECT realm_mtime, realm_size FROM scans WHERE status='done' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if not force and last and (last["realm_mtime"], last["realm_size"]) == (current.mtime, current.size):
        probed = fill_durations(conn, lazer_dir, progress)  # e.g. ffprobe installed since last scan
        return ScanResult(status="skipped", durations_probed=probed)

    with conn:
        scan_id = conn.execute(
            "INSERT INTO scans(started_at, status) VALUES(?, 'running')", (now_iso(),)
        ).lastrowid
    try:
        if progress:
            progress("copy", 0, 1)
        realm_copy, copied_stat = copy_realm(lazer_dir, work_dir)
        if progress:
            progress("export", 0, 1)
        export = helper.export(realm_copy, work_dir / "export.json")
        diff, records = apply_export(conn, export, scan_id, include_protected)
        probed = fill_durations(conn, lazer_dir, progress)
        warnings = export.get("warnings") or []
        with conn:
            conn.execute(
                """UPDATE scans SET finished_at=?, status='done', realm_mtime=?, realm_size=?,
                       sets_total=?, sets_new=?, sets_gone=?, warnings=? WHERE id=?""",
                (now_iso(), copied_stat.mtime, copied_stat.size, len(records), len(diff.new),
                 len(diff.gone), json.dumps(warnings, ensure_ascii=False), scan_id),
            )
        return ScanResult(
            status="done", scan_id=scan_id, total=len(records), new=len(diff.new), gone=len(diff.gone),
            returned=len(diff.returned), durations_probed=probed, warnings=warnings,
        )
    except Exception as e:
        with conn:
            conn.execute(
                "UPDATE scans SET finished_at=?, status='error', error=? WHERE id=?", (now_iso(), str(e), scan_id)
            )
        raise
