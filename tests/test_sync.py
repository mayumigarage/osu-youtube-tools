import threading
from datetime import datetime, timedelta, timezone

import pytest

from fake_youtube import FakeService, http_error
from osu2yt import db, sync
from osu2yt.youtube import playlists, quota
from osu2yt.youtube.client import YouTubeClient

PL = "PL1"


class FakePlaylist:
    """A YouTube playlist that remembers inserts. quota_after: inserts allowed before quotaExceeded."""

    def __init__(self, items=(), private=(), gone=(), fail=(), quota_after=None):
        self.items = list(items)
        self.private, self.gone, self.fail = set(private), set(gone), set(fail)
        self.quota_after = quota_after
        self.inserted = []

    def handlers(self):
        return {"playlistItems.list": self.list, "playlistItems.insert": self.insert, "videos.list": self.videos}

    def list(self, kw):
        return {"items": [{"id": f"i-{v}", "contentDetails": {"videoId": v}} for v in self.items]}

    def insert(self, kw):
        vid = kw["body"]["snippet"]["resourceId"]["videoId"]
        assert kw["body"]["snippet"]["playlistId"] == PL
        if self.quota_after is not None and len(self.inserted) >= self.quota_after:
            return http_error(403, "quotaExceeded")
        if vid in self.gone:
            return http_error(404, "videoNotFound")
        if vid in self.fail:
            return http_error(403, "forbidden")
        self.items.append(vid)
        self.inserted.append(vid)
        return {"id": f"i-{vid}"}

    def videos(self, kw):
        out = []
        for v in kw["id"].split(","):
            if v in self.gone:
                continue
            out.append({"id": v, "status": {"privacyStatus": "private" if v in self.private else "public"}})
        return {"items": out}


def add_set(conn, key, vid, status="confirmed", added=None):
    conn.execute(
        """INSERT INTO beatmapsets(key, title, title_unicode, artist, artist_unicode, in_library, match_status,
               chosen_video_id, chosen_by, date_added) VALUES(?, ?, ?, 'a', 'a', 1, ?, ?, 'auto', ?)""",
        (key, f"t{key}", f"t{key}", status, vid, added or key),
    )
    conn.commit()


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.execute("INSERT INTO playlists(id, title) VALUES(?, 'osu! songs')", (PL,))
    playlists.set_active(c, PL)
    add_set(c, "o:1", "v1", added="2025-01-01")
    add_set(c, "o:2", "v2", added="2025-01-02")
    add_set(c, "o:3", "v1", added="2025-01-03")      # same video as o:1
    add_set(c, "o:4", "v4", status="needs_review", added="2025-01-04")
    add_set(c, "o:5", "v5", added="2025-01-05")
    c.commit()
    yield c
    c.close()


def client(conn, fake):
    return YouTubeClient(FakeService(fake.handlers()), conn, sleep=lambda s: None)


def run(conn, fake, **kw):
    return sync.run_sync(conn, client(conn, fake), PL, **kw)


def queue(conn, run_id):
    return {r["video_id"]: r["state"] for r in conn.execute(
        "SELECT video_id, state FROM sync_queue WHERE sync_run_id=?", (run_id,))}


# --- plan / estimate --------------------------------------------------------------------------------

def test_plan_dedups_videos_and_excludes_known_cases(conn):
    conn.execute("INSERT INTO playlist_items(playlist_id, video_id, present) VALUES(?, 'v2', 0)", (PL,))  # removed
    conn.execute("INSERT INTO video_checks(video_id, availability, checked_at) VALUES('v5', 'private', ?)",
                 (db.now_iso(),))
    p = sync.plan(conn, PL)
    assert [(it.video_id, it.set_keys) for it in p.to_add] == [("v1", ["o:1", "o:3"])]
    assert (p.confirmed_sets, p.unique_videos, p.removed_remote, p.unavailable, p.needs_review) == (4, 3, 1, 1, 1)


def test_estimate_counts_pull_check_and_inserts(conn):
    conn.execute("UPDATE playlists SET item_count=120 WHERE id=?", (PL,))
    conn.execute("INSERT INTO video_checks VALUES('v1', 'ok', ?)", (db.now_iso(),))  # fresh → no recheck
    e = sync.estimate(conn, sync.plan(conn, PL))
    assert (e["count"], e["pull_units"], e["check_units"], e["insert_units"]) == (3, 3, 1, 150)
    assert e["total_units"] == 154 and e["inserts_today"] == 3 and e["days"] == 1
    assert sync.estimate(conn, sync.plan(conn, PL), max_add=1)["count"] == 1


def test_estimate_spreads_over_days(conn):
    db.set_setting(conn, "quota_limit", "450")      # (450 - 300 margin - pull) // 50 = 2 per day
    for i in range(6, 12):
        add_set(conn, f"o:{i}", f"v{i}")
    e = sync.estimate(conn, sync.plan(conn, PL))
    assert e["count"] == 9 and e["inserts_today"] == 2 and e["per_day"] == 2 and e["days"] == 1 + 4


# --- running --------------------------------------------------------------------------------------------

def test_adds_missing_videos_once_in_library_order(conn):
    fake = FakePlaylist(items=["v2", "vOther"])
    r = run(conn, fake)
    assert fake.inserted == ["v1", "v5"]
    assert (r.status, r.planned, r.added, r.remaining) == ("done", 2, 2, 0)
    row = conn.execute("SELECT * FROM playlist_items WHERE video_id='v1'").fetchone()
    assert (row["origin"], row["set_key"], row["sync_run_id"], row["present"]) == ("tool", "o:1", r.run_id, 1)
    assert r.quota_used == 1 + 1 + 100       # pull + videos.list + 2 inserts
    hist = sync.runs(conn)[0]
    assert (hist["status"], hist["added"], hist["quota_used"], hist["trigger"]) == ("done", 2, 102, "manual")

    r2 = run(conn, fake)                        # nothing new: no inserts
    assert (r2.planned, r2.added) == (0, 0) and fake.inserted == ["v1", "v5"]


def test_new_song_is_added_as_difference(conn):
    fake = FakePlaylist()
    run(conn, fake)
    add_set(conn, "o:9", "v9", added="2026-01-01")
    r = run(conn, fake)
    assert (r.added, fake.inserted) == (1, ["v1", "v2", "v5", "v9"])


def test_video_removed_on_youtube_is_not_readded(conn):
    fake = FakePlaylist()
    run(conn, fake)
    fake.items.remove("v1")                     # user removed it in the YouTube UI
    r = run(conn, fake)
    assert r.added == 0 and fake.inserted == ["v1", "v2", "v5"]
    assert r.pull["went_missing"] == 1
    assert sync.plan(conn, PL).removed_remote == 1


def test_video_added_manually_on_youtube_counts_as_present(conn):
    fake = FakePlaylist(items=["v5"])
    run(conn, fake)
    assert fake.inserted == ["v1", "v2"]


def test_unplayable_videos_are_skipped_and_sent_back_to_review(conn):
    fake = FakePlaylist(private=["v5"])
    r = run(conn, fake)
    assert fake.inserted == ["v1", "v2"] and r.unavailable == 1
    assert queue(conn, r.run_id)["v5"] == "unavailable"
    row = conn.execute("SELECT match_status, review_reason FROM beatmapsets WHERE key='o:5'").fetchone()
    assert tuple(row) == ("needs_review", "unavailable:private")


def test_video_gone_at_insert_time(conn):
    conn.execute("INSERT INTO video_checks VALUES('v2', 'ok', ?)", (db.now_iso(),))   # checked recently: no recheck
    fake = FakePlaylist(gone=["v2"])
    fake.videos = lambda kw: {"items": [{"id": v, "status": {"privacyStatus": "public"}} for v in kw["id"].split(",")]}
    r = run(conn, fake)
    assert (r.added, r.unavailable) == (2, 1)
    assert conn.execute("SELECT review_reason FROM beatmapsets WHERE key='o:2'").fetchone()[0] == "unavailable:deleted"


def test_quota_exceeded_pauses_and_next_run_resumes(conn):
    fake = FakePlaylist(quota_after=1)
    r = run(conn, fake)
    assert (r.status, r.added, r.remaining) == ("paused_quota", 1, 2)
    assert "クォータ" in r.error
    assert sync.resume_due(conn, PL) is None                                   # same PT day
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    assert sync.resume_due(conn, PL, now=tomorrow)["id"] == r.run_id

    fake.quota_after = None
    r2 = run(conn, fake, trigger="auto_resume")
    assert (r2.status, r2.added) == ("done", 2) and fake.inserted == ["v1", "v2", "v5"]
    assert set(queue(conn, r.run_id).values()) == {"added", "carried_over"}
    assert sync.resume_due(conn, PL, now=tomorrow) is None


def test_local_budget_stops_before_calling_the_api(conn):
    db.set_setting(conn, "quota_limit", "351")   # 51 usable: pull + check, then not enough for an insert
    fake = FakePlaylist()
    r = run(conn, fake)
    assert r.status == "paused_quota" and fake.inserted == [] and r.remaining == 3
    assert quota.used(conn) == 2


def test_dry_run_inserts_nothing(conn):
    fake = FakePlaylist(items=["v2"], private=["v5"])
    r = run(conn, fake, dry_run=True)
    assert fake.inserted == [] and (r.status, r.planned, r.unavailable) == ("done", 2, 1)
    assert queue(conn, r.run_id) == {"v1": "planned", "v5": "unavailable"}
    assert conn.execute("SELECT dry_run FROM sync_runs WHERE id=?", (r.run_id,)).fetchone()[0] == 1


def test_max_add_limits_this_run(conn):
    fake = FakePlaylist()
    r = run(conn, fake, max_add=2)
    assert fake.inserted == ["v1", "v2"] and r.planned == 2


def test_failures_are_recorded_and_run_continues(conn):
    fake = FakePlaylist(fail=["v2"])
    r = run(conn, fake)
    assert (r.status, r.added, r.failed) == ("done", 2, 1)
    assert conn.execute("SELECT last_error FROM sync_queue WHERE video_id='v2'").fetchone()[0]


def test_consecutive_failures_abort(conn):
    for i in range(6, 12):
        add_set(conn, f"o:{i}", f"v{i}")
    fake = FakePlaylist(fail=[f"v{i}" for i in (1, 2, 5, 6, 7, 8, 9, 10, 11)])
    with pytest.raises(sync.SyncError):
        run(conn, fake)
    last = sync.runs(conn)[0]
    assert last["status"] == "error" and last["failed"] == 5


def test_cancel_stops_before_next_insert(conn):
    ev = threading.Event()
    fake = FakePlaylist()
    orig = fake.insert

    def insert(kw):
        ev.set()
        return orig(kw)

    fake.insert = insert
    r = run(conn, fake, cancel=ev)
    assert r.status == "cancelled" and fake.inserted == ["v1"]


def test_missing_playlist(conn):
    fake = FakePlaylist()
    fake.list = lambda kw: http_error(404, "playlistNotFound")
    with pytest.raises(sync.SyncError, match="プレイリストが見つかりません"):
        run(conn, fake)


def test_mark_interrupted(conn):
    conn.execute("INSERT INTO sync_runs(playlist_id, status, dry_run) VALUES(?, 'running', 0)", (PL,))
    conn.commit()
    assert sync.mark_interrupted(conn) == 1
    assert sync.runs(conn)[0]["status"] == "interrupted"


def test_run_detail_lists_items(conn):
    r = run(conn, FakePlaylist())
    d = sync.run_detail(conn, r.run_id)
    assert [(i["video_id"], i["state"], i["title_unicode"]) for i in d["items"]] == [
        ("v1", "added", "to:1"), ("v2", "added", "to:2"), ("v5", "added", "to:5")]
    assert sync.run_detail(conn, 999) is None
