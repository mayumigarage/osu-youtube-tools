import pytest

from fake_youtube import FakeService
from osu2yt import db
from osu2yt.logging_setup import mask
from osu2yt.matching import matcher
from osu2yt.youtube import playlists
from osu2yt.youtube.client import YouTubeClient


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    for key, vid, status in [("o:1", "v1", "confirmed"), ("o:2", "v2", "confirmed"), ("o:3", "v3", "needs_review"),
                             ("o:4", "v1", "confirmed")]:
        c.execute(
            """INSERT INTO beatmapsets(key, title, title_unicode, artist, artist_unicode, in_library, match_status,
                   chosen_video_id, chosen_by, date_added) VALUES(?, 't', 't', 'a', 'a', 1, ?, ?, 'auto', ?)""",
            (key, status, vid, key),
        )
    c.commit()
    yield c
    c.close()


def items(*pairs):
    return {"items": [{"id": i, "contentDetails": {"videoId": v}} for i, v in pairs]}


def client_for(conn, handlers):
    return YouTubeClient(FakeService(handlers), conn, sleep=lambda s: None)


def present(conn, pid="PL1"):
    return {r[0]: r[1] for r in conn.execute("SELECT video_id, present FROM playlist_items WHERE playlist_id=?", (pid,))}


def test_create_sets_active(conn):
    c = client_for(conn, {"playlists.insert": [{"id": "PL1", "snippet": {"title": "x"}, "status": {"privacyStatus": "unlisted"}}]})
    playlists.create(conn, c, "x", "unlisted")
    row = playlists.active(conn)
    assert (row["id"], row["privacy"], row["created_by_tool"]) == ("PL1", "unlisted", 1)


def test_use_unknown_playlist_id(conn):
    playlists.set_active(conn, "PLmanual")
    assert playlists.active(conn)["id"] == "PLmanual"


def test_pull_items_tracks_new_missing_and_duplicates(conn):
    c = client_for(conn, {"playlistItems.list": [
        items(("i1", "v1"), ("i2", "vX"), ("i3", "v1")),
        items(("i2", "vX")),
        items(("i1", "v1"), ("i2", "vX")),
    ]})
    r = playlists.pull_items(conn, c, "PL1")
    assert (r.total, r.new_remote, r.went_missing, r.duplicates) == (3, 2, 0, 1)
    links = dict(conn.execute("SELECT video_id, set_key FROM playlist_items").fetchall())
    assert links == {"v1": "o:1", "vX": None}   # linked to the earliest set that chose the video

    r = playlists.pull_items(conn, c, "PL1")       # v1 removed on YouTube
    assert (r.new_remote, r.went_missing) == (0, 1)
    assert present(conn) == {"v1": 0, "vX": 1}

    r = playlists.pull_items(conn, c, "PL1")       # v1 added back
    assert present(conn) == {"v1": 1, "vX": 1}
    assert conn.execute("SELECT item_count FROM playlists WHERE id='PL1'").fetchone()[0] == 2


def test_check_chosen_videos_flags_unplayable(conn):
    def handler(kw):
        out = []
        for v in kw["id"].split(","):
            if v == "v2":
                out.append({"id": v, "status": {"privacyStatus": "private"}})
            elif v != "v3":   # v3 deleted
                out.append({"id": v, "status": {"privacyStatus": "public"}})
        return {"items": out}

    svc = FakeService({"videos.list": handler})
    c = YouTubeClient(svc, conn, sleep=lambda s: None)
    r = playlists.check_chosen_videos(conn, c)
    assert r.checked == 3 and r.unavailable == {"v2": "private", "v3": "deleted"}
    assert r.sets_flagged == 2
    rows = dict(conn.execute("SELECT key, match_status || '/' || review_reason FROM beatmapsets").fetchall())
    assert rows["o:2"] == "needs_review/unavailable:private"
    assert rows["o:3"] == "needs_review/unavailable:deleted"
    assert conn.execute("SELECT match_status FROM beatmapsets WHERE key='o:1'").fetchone()[0] == "confirmed"

    # recently checked videos are not checked again (no quota spent)
    assert playlists.check_chosen_videos(conn, c).checked == 0
    assert svc.count("videos.list") == 1
    # and dead videos are excluded from re-matching
    assert matcher.unplayable_ids(conn) == {"v2", "v3"}


def test_secrets_are_masked():
    line = 'token ya29.a0AfH6SMBx-secret refresh 1//0gAbCdEfGhIjKlMnOpQrStUv {"client_secret": "GOCSPX-abc", "code": "4/xyz"}'
    out = mask(line)
    for secret in ("ya29.a0AfH6SMBx-secret", "1//0gAbCdEfGhIjKlMnOpQrStUv", "GOCSPX-abc", "4/xyz"):
        assert secret not in out
    assert mask("normal message") == "normal message"
