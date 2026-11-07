import threading

import pytest
from fastapi.testclient import TestClient

from osu2yt import config, db
from osu2yt.app import create_app
from osu2yt.matching import matcher
from osu2yt.matching.search import VideoInfo

H = {"X-Osu2yt": "1"}


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "WORK_DIR", tmp_path / "work")
    path = tmp_path / "t.db"
    conn = db.connect(path)
    rows = [
        # key, title_u, artist_u, status, video, score, reason
        ("o:1", "ヒューマノイド", "ずっと真夜中でいいのに。", "confirmed", "vid00000001", 1.0, None, "2025-01-03"),
        ("o:2", "勘冴えて悔しいわ", "ずっと真夜中でいいのに。", "needs_review", "vid00000002", 0.6, "low_score", "2025-01-02"),
        ("o:3", "お願いマッスル", "紗倉ひびき", "unmatched", None, None, None, "2025-01-01"),
        ("o:4", "ヒューマノイド (TV Size)", "ずっと真夜中でいいのに。", "confirmed", "vid00000001", 0.9, None, "2025-01-04"),
    ]
    for key, t, a, st, vid, sc, reason, added in rows:
        conn.execute(
            """INSERT INTO beatmapsets(key, online_id, title, title_unicode, artist, artist_unicode, duration_sec,
                   in_library, match_status, chosen_video_id, chosen_by, match_score, review_reason, date_added, variants_json)
               VALUES(?, ?, ?, ?, ?, ?, 260, 1, ?, ?, 'auto', ?, ?, ?, '[]')""",
            (key, int(key[2:]), t, t, a, a, st, vid, sc, reason, added),
        )
    for key, vid, title, score in [("o:1", "vid00000001", "ずっと真夜中でいいのに。『ヒューマノイド』MV", 1.0),
                                   ("o:2", "vid00000002", "勘冴えて悔しいわ live", 0.6),
                                   ("o:2", "vid00000003", "勘冴えて悔しいわ MV", 0.55)]:
        conn.execute(
            """INSERT INTO candidates(set_key, video_id, rank, query, title, channel, channel_verified, duration_sec,
                   thumbnail_url, score, score_detail) VALUES(?, ?, 1, 'q', ?, 'ch', 1, 261, 't', ?, '{"parts": {}, "notes": []}')""",
            (key, vid, title, score),
        )
    conn.execute("INSERT INTO playlists(id, title) VALUES('PL1', 'osu! songs')")
    conn.execute("INSERT INTO settings(key, value) VALUES('active_playlist_id', 'PL1')")
    conn.commit()
    conn.close()
    return create_app(path)


@pytest.fixture
def client(app):
    return TestClient(app)


def add_to_playlist(app, vid):
    conn = db.connect(app.state.db_path)
    conn.execute("INSERT INTO playlist_items(playlist_id, video_id, origin, present) VALUES('PL1', ?, 'tool', 1)", (vid,))
    conn.commit()
    conn.close()


class TestSecurity:
    def test_index_has_csp(self, client):
        r = client.get("/")
        assert r.status_code == 200 and "osu2yt" in r.text
        assert "frame-src https://www.youtube-nocookie.com" in r.headers["content-security-policy"]

    def test_foreign_host_rejected(self, client):  # DNS rebinding
        assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 403
        assert client.get("/api/status", headers={"Host": "127.0.0.1:8765"}).status_code == 200
        assert client.get("/api/status", headers={"Host": "localhost:8765"}).status_code == 200

    def test_mutation_requires_header(self, client):
        assert client.post("/api/library/o:3/skip").status_code == 403
        assert client.post("/api/library/o:3/skip", headers=H).status_code == 200

    def test_foreign_origin_rejected(self, client):
        r = client.post("/api/library/o:3/skip", headers=H | {"Origin": "https://evil.example"})
        assert r.status_code == 403
        r = client.post("/api/library/o:3/unskip", headers=H | {"Origin": "http://127.0.0.1:8765"})
        assert r.status_code == 200


def test_status(client):
    s = client.get("/api/status").json()
    assert s["auth"]["client_secret"] is False and s["auth"]["logged_in"] is False
    assert s["playlist"]["id"] == "PL1"
    assert s["quota"]["inserts_possible"] == 194


class TestLibrary:
    def test_counts_and_added_state(self, app, client):
        add_to_playlist(app, "vid00000001")
        d = client.get("/api/library").json()
        assert d["counts"] == {"added": 2, "needs_review": 1, "unmatched": 1, "all": 4}
        states = {i["key"]: i["state"] for i in d["items"]}
        assert states == {"o:1": "added", "o:4": "added", "o:2": "needs_review", "o:3": "unmatched"}
        assert [i["key"] for i in d["items"]] == ["o:4", "o:1", "o:2", "o:3"]   # newest first

    def test_filters(self, client):
        d = client.get("/api/library", params={"view": "needs_review"}).json()
        assert [i["key"] for i in d["items"]] == ["o:2"]
        assert d["reasons"] == {"low_score": 1}
        assert client.get("/api/library", params={"q": "マッスル"}).json()["total"] == 1
        assert client.get("/api/library", params={"q": "o:2"}).json()["total"] == 1
        assert client.get("/api/library", params={"view": "bogus"}).status_code == 422

    def test_pagination_and_keys(self, client):
        d = client.get("/api/library", params={"per_page": 3, "page": 2}).json()
        assert d["total"] == 4 and len(d["items"]) == 1
        assert client.get("/api/library/keys", params={"sort": "added_asc"}).json() == ["o:3", "o:2", "o:1", "o:4"]

    def test_detail(self, client):
        d = client.get("/api/library/o:1").json()
        assert d["video"]["id"] == "vid00000001"
        assert [c["video_id"] for c in d["candidates"]] == ["vid00000001"] and d["candidates"][0]["chosen"]
        assert [s["key"] for s in d["same_video_sets"]] == ["o:4"]
        assert client.get("/api/library/o:999").status_code == 404

    def test_choose_confirm_skip(self, client):
        d = client.post("/api/library/o:2/choose", json={"video": "https://youtu.be/vid00000003"}, headers=H).json()
        assert (d["state"], d["chosen_by"], d["video"]["id"]) == ("confirmed", "user", "vid00000003")
        assert client.post("/api/library/o:2/choose", json={"video": "nope"}, headers=H).status_code == 400
        assert client.post("/api/library/o:2/skip", headers=H).json()["state"] == "skipped"
        assert client.post("/api/library/o:2/unskip", headers=H).json()["state"] == "confirmed"
        assert client.post("/api/library/o:3/confirm", headers=H).status_code == 400   # nothing chosen

    def test_rematch_single(self, client, monkeypatch):
        found = [VideoInfo("vid00000009", "紗倉ひびき お願いマッスル MV", "紗倉ひびき", None, True, 260)]
        monkeypatch.setattr(matcher.search_mod, "search", lambda q, n: found)
        d = client.post("/api/library/o:3/rematch", headers=H).json()
        assert d["video"]["id"] == "vid00000009" and d["candidates"]


class TestSettings:
    def test_defaults_and_update(self, client):
        s = client.get("/api/settings").json()
        assert s["region"] == "JP" and s["quota_limit"] == "10000"
        s = client.put("/api/settings", json={"region": "US", "quota_margin": 0, "include_protected": True}, headers=H).json()
        assert (s["region"], s["quota_margin"], s["include_protected"]) == ("US", "0", "1")

    def test_validation(self, client, tmp_path):
        assert client.put("/api/settings", json={"region": "japan"}, headers=H).status_code == 422
        assert client.put("/api/settings", json={"lazer_path": str(tmp_path / "nope")}, headers=H).status_code == 400
        (tmp_path / "osu").mkdir()
        (tmp_path / "osu" / "client.realm").write_bytes(b"x")
        r = client.put("/api/settings", json={"lazer_path": str(tmp_path / "osu")}, headers=H)
        assert r.status_code == 200


class TestJobs:
    def test_match_job_runs_in_background(self, app, client, monkeypatch):
        found = [VideoInfo("vid00000009", "紗倉ひびき お願いマッスル MV", "紗倉ひびき", None, True, 260)]
        monkeypatch.setattr(matcher.search_mod, "search", lambda q, n: found)
        j = client.post("/api/match", json={"scope": "unmatched", "workers": 1}, headers=H).json()
        app.state.runner.wait(app.state.runner.get(j["id"]))
        j = client.get(f"/api/jobs/{j['id']}").json()
        assert j["status"] == "done" and j["result"]["total"] == 1
        assert client.get("/api/library", params={"view": "unmatched"}).json()["total"] == 0

    def test_only_one_job_at_a_time(self, app, client):
        gate = threading.Event()
        job = app.state.runner.start("x", "テスト", lambda job, conn: gate.wait(5))
        r = client.post("/api/match", json={"scope": "unmatched"}, headers=H)
        assert r.status_code == 409
        assert client.post(f"/api/jobs/{job.id}/cancel", headers=H).json()["cancel_requested"]
        gate.set()
        app.state.runner.wait(job)
        assert client.get(f"/api/jobs/{job.id}").json()["status"] == "cancelled"

    def test_failed_job_reports_masked_error(self, app, client):
        def boom(job, conn):
            raise RuntimeError("bad token ya29.secretsecret")
        job = app.state.runner.start("x", "テスト", boom)
        app.state.runner.wait(job)
        j = client.get(f"/api/jobs/{job.id}").json()
        assert j["status"] == "error" and "ya29.secret" not in j["error"]


def test_youtube_endpoints_require_login(client):
    assert client.post("/api/playlists/refresh", headers=H).status_code == 401
    assert client.post("/api/auth/login", headers=H).status_code == 400   # no client_secret.json
    assert client.get("/api/playlists").json()["active"] == "PL1"


# --- sync ------------------------------------------------------------------------------------------

class _Creds:
    valid = True


@pytest.fixture
def fake_youtube(monkeypatch):
    from fake_youtube import FakeService
    from test_sync import FakePlaylist

    from osu2yt.youtube import auth
    from osu2yt.youtube.client import YouTubeClient

    fake = FakePlaylist()
    fake.items = []
    monkeypatch.setattr(auth, "load_credentials", lambda refresh=True: _Creds())
    monkeypatch.setattr(auth, "require_credentials", lambda: _Creds())
    monkeypatch.setattr(YouTubeClient, "from_saved_credentials", classmethod(
        lambda cls, conn, **kw: cls(FakeService(fake.handlers()), conn, sleep=lambda s: None)))
    return fake


class TestSync:
    def test_preview_is_local(self, client):
        d = client.get("/api/sync/preview").json()
        assert d["summary"]["confirmed_sets"] == 2 and d["summary"]["unique_videos"] == 1
        assert d["summary"]["needs_review"] == 1 and d["estimate"]["count"] == 1
        assert [i["set_count"] for i in d["items"]] == [2]
        assert d["logged_in"] is False and d["last_run"] is None and d["auto_resume"] is True

    def test_requires_login(self, client):
        assert client.post("/api/sync", json={}, headers=H).status_code == 401
        assert client.get("/api/sync/runs").json() == []
        assert client.get("/api/sync/runs/1").status_code == 404

    def test_sync_job_and_history(self, app, client, fake_youtube):
        fake_youtube.items = []
        j = client.post("/api/sync", json={"dry_run": True}, headers=H).json()
        app.state.runner.wait(app.state.runner.get(j["id"]))
        assert fake_youtube.inserted == []
        j = client.post("/api/sync", json={}, headers=H).json()
        app.state.runner.wait(app.state.runner.get(j["id"]))
        j = client.get(f"/api/jobs/{j['id']}").json()
        assert j["status"] == "done" and j["result"]["added"] == 1
        assert fake_youtube.inserted == ["vid00000001"]
        runs = client.get("/api/sync/runs").json()
        assert [(r["dry_run"], r["status"], r["added"]) for r in runs] == [(0, "done", 1), (1, "done", 0)]
        d = client.get(f"/api/sync/runs/{runs[0]['id']}").json()
        assert d["items"][0]["state"] == "added"
        assert client.get("/api/library").json()["counts"]["added"] == 2   # both sets share the video

    def test_auto_resume_after_reset(self, app, client, fake_youtube):
        conn = db.connect(app.state.db_path)
        conn.execute("""INSERT INTO sync_runs(playlist_id, status, dry_run, pt_date, added)
                        VALUES('PL1', 'paused_quota', 0, '2000-01-01', 0)""")
        conn.commit()
        sched = app.state.auto_resume
        db.set_setting(conn, "auto_resume", "0")
        assert sched.tick() is None
        db.set_setting(conn, "auto_resume", "1")
        conn.close()
        job = sched.tick()
        assert job is not None
        app.state.runner.wait(job)
        assert fake_youtube.inserted == ["vid00000001"]
        assert client.get("/api/sync/runs").json()[0]["trigger"] == "auto_resume"
        assert sched.tick() is None    # finished: nothing left to resume


def test_auto_resume_setting(client):
    assert client.get("/api/settings").json()["auto_resume"] == "1"
    assert client.put("/api/settings", json={"auto_resume": False}, headers=H).json()["auto_resume"] == "0"
    assert client.get("/api/sync/preview").json()["auto_resume"] is False
