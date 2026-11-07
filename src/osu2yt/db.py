"""SQLite access and schema migrations (tracked with PRAGMA user_version)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_V1 = """
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE scans (
  id INTEGER PRIMARY KEY,
  started_at TEXT, finished_at TEXT,
  realm_mtime REAL, realm_size INTEGER,
  status TEXT,                               -- running/done/error
  sets_total INTEGER, sets_new INTEGER, sets_gone INTEGER,
  warnings TEXT, error TEXT);

CREATE TABLE audio_durations (audio_hash TEXT PRIMARY KEY, duration_sec REAL);

CREATE TABLE beatmapsets (
  key TEXT PRIMARY KEY,                      -- 'o:<onlineId>' | 'g:<guid>'
  online_id INTEGER, realm_guid TEXT,
  title TEXT, title_unicode TEXT, artist TEXT, artist_unicode TEXT, source TEXT, tags TEXT,
  audio_hash TEXT, map_length_sec REAL,
  duration_sec REAL, duration_source TEXT,   -- 'audio' | 'map'
  variants_json TEXT, is_pack INTEGER NOT NULL DEFAULT 0,
  date_added TEXT, first_seen_scan INTEGER, last_seen_scan INTEGER,
  in_library INTEGER NOT NULL DEFAULT 1,
  match_status TEXT NOT NULL DEFAULT 'unmatched',  -- unmatched/needs_review/confirmed/skipped
  chosen_video_id TEXT, chosen_by TEXT,      -- auto/user/manual
  match_score REAL, review_reason TEXT, matched_at TEXT, updated_at TEXT);
CREATE INDEX ix_sets_status ON beatmapsets(match_status, in_library);

CREATE TABLE candidates (
  set_key TEXT NOT NULL REFERENCES beatmapsets(key) ON DELETE CASCADE,
  video_id TEXT NOT NULL, rank INTEGER, query TEXT,
  title TEXT, channel TEXT, channel_id TEXT, channel_verified INTEGER,
  duration_sec REAL, thumbnail_url TEXT,
  score REAL, score_detail TEXT, fetched_at TEXT,
  PRIMARY KEY (set_key, video_id));

CREATE TABLE video_checks (
  video_id TEXT PRIMARY KEY, availability TEXT, checked_at TEXT);

CREATE TABLE playlists (
  id TEXT PRIMARY KEY, title TEXT, privacy TEXT, created_by_tool INTEGER, created_at TEXT);

CREATE TABLE playlist_items (
  playlist_id TEXT NOT NULL, video_id TEXT NOT NULL, playlist_item_id TEXT,
  set_key TEXT, origin TEXT, added_at TEXT, sync_run_id INTEGER,
  PRIMARY KEY (playlist_id, video_id));

CREATE TABLE sync_runs (
  id INTEGER PRIMARY KEY, playlist_id TEXT, started_at TEXT, finished_at TEXT,
  status TEXT, dry_run INTEGER, planned INTEGER, added INTEGER, skipped_dup INTEGER,
  failed INTEGER, quota_used INTEGER, error TEXT);

CREATE TABLE sync_queue (
  sync_run_id INTEGER NOT NULL, set_key TEXT NOT NULL, video_id TEXT,
  state TEXT, attempts INTEGER DEFAULT 0, last_error TEXT,
  PRIMARY KEY (sync_run_id, set_key));

CREATE TABLE quota_log (
  id INTEGER PRIMARY KEY, ts TEXT, pt_date TEXT, method TEXT, units INTEGER);
CREATE INDEX ix_quota_date ON quota_log(pt_date);
"""

SCHEMA_V2 = """
-- present=0: the item was in the playlist before but is gone now (removed on YouTube by the user).
-- Such videos are not re-added automatically.
ALTER TABLE playlist_items ADD COLUMN present INTEGER NOT NULL DEFAULT 1;
ALTER TABLE playlist_items ADD COLUMN last_seen_at TEXT;
ALTER TABLE playlists ADD COLUMN item_count INTEGER;
ALTER TABLE playlists ADD COLUMN last_pulled_at TEXT;
"""

SCHEMA_V3 = """
-- sync bookkeeping: why a run started, how many were unplayable, how many remain after a pause
ALTER TABLE sync_runs ADD COLUMN trigger TEXT;
ALTER TABLE sync_runs ADD COLUMN unavailable INTEGER;
ALTER TABLE sync_runs ADD COLUMN remaining INTEGER;
ALTER TABLE sync_runs ADD COLUMN pt_date TEXT;
ALTER TABLE sync_queue ADD COLUMN position INTEGER;
ALTER TABLE sync_queue ADD COLUMN updated_at TEXT;
CREATE INDEX ix_sync_queue_run ON sync_queue(sync_run_id, position);
"""

MIGRATIONS = [SCHEMA_V1, SCHEMA_V2, SCHEMA_V3]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 10000")  # web requests and background jobs share the file
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for target, script in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(script)
            conn.execute(f"PRAGMA user_version = {target}")


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
