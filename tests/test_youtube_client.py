import socket

import pytest

from fake_youtube import FakeService, http_error
from osu2yt import db
from osu2yt.youtube import quota
from osu2yt.youtube.client import (
    NoChannel,
    NotFound,
    QuotaExceeded,
    YouTubeClient,
    YouTubeError,
    availability,
)


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def make(conn, handlers, **kw):
    sleeps = []
    svc = FakeService(handlers)
    client = YouTubeClient(svc, conn, base_delay=0.01, sleep=sleeps.append, **kw)
    return client, svc, sleeps


INSERTED = {"id": "item1"}


def test_success_records_quota(conn):
    client, svc, _ = make(conn, {"playlistItems.insert": [INSERTED]})
    assert client.insert_playlist_item("PL1", "vid") == "item1"
    body = svc.calls[0][1]["body"]["snippet"]
    assert body == {"playlistId": "PL1", "resourceId": {"kind": "youtube#video", "videoId": "vid"}}
    assert quota.used(conn) == 50


def test_transient_errors_are_retried_with_backoff(conn):
    client, svc, sleeps = make(conn, {"playlistItems.insert": [
        http_error(503, "backendError"), http_error(409, "SERVICE_UNAVAILABLE"), socket.timeout(), INSERTED]})
    assert client.insert_playlist_item("PL1", "vid") == "item1"
    assert svc.count("playlistItems.insert") == 4
    assert len(sleeps) == 3 and sleeps[0] < sleeps[1] < sleeps[2]   # exponential
    assert quota.used(conn) == 200  # every attempt is charged


def test_gives_up_after_max_attempts(conn):
    client, svc, _ = make(conn, {"playlistItems.insert": lambda kw: http_error(500, "backendError")}, max_attempts=3)
    with pytest.raises(YouTubeError):
        client.insert_playlist_item("PL1", "vid")
    assert svc.count("playlistItems.insert") == 3


@pytest.mark.parametrize("reason", ["quotaExceeded", "dailyLimitExceeded"])
def test_quota_exceeded_stops_immediately(conn, reason):
    client, svc, sleeps = make(conn, {"playlistItems.insert": [http_error(403, reason)]})
    with pytest.raises(QuotaExceeded):
        client.insert_playlist_item("PL1", "vid")
    assert svc.count("playlistItems.insert") == 1 and sleeps == []


def test_non_transient_error_is_not_retried(conn):
    client, svc, _ = make(conn, {"playlistItems.insert": [http_error(403, "forbidden")]})
    with pytest.raises(YouTubeError):
        client.insert_playlist_item("PL1", "vid")
    assert svc.count("playlistItems.insert") == 1


def test_not_found(conn):
    client, _, _ = make(conn, {"playlistItems.list": [http_error(404, "playlistNotFound")]})
    with pytest.raises(NotFound):
        client.list_playlist_items("PLx")


@pytest.mark.parametrize("reason", ["channelNotFound", "youtubeSignupRequired"])
def test_account_without_channel(conn, reason):
    client, svc, _ = make(conn, {"playlists.list": [http_error(404, reason)]})
    with pytest.raises(NoChannel, match="create_channel"):
        client.list_my_playlists()
    assert svc.count("playlists.list") == 1


def test_local_budget_blocks_before_calling_api(conn):
    db.set_setting(conn, "quota_limit", "100")
    db.set_setting(conn, "quota_margin", "40")
    client, svc, _ = make(conn, {"playlistItems.insert": [INSERTED, INSERTED]})
    client.insert_playlist_item("PL1", "a")      # 50 of 60 usable
    with pytest.raises(QuotaExceeded):
        client.insert_playlist_item("PL1", "b")
    assert svc.count("playlistItems.insert") == 1


def test_list_playlist_items_paginates(conn):
    pages = [
        {"items": [{"id": "i1", "contentDetails": {"videoId": "v1"}}, {"id": "i2", "contentDetails": {"videoId": "v2"}}],
         "nextPageToken": "p2"},
        {"items": [{"id": "i3", "contentDetails": {"videoId": "v1"}}, {"id": "i4", "contentDetails": {}}]},
    ]
    client, svc, _ = make(conn, {"playlistItems.list": pages})
    items = client.list_playlist_items("PL1")
    assert [(i.item_id, i.video_id) for i in items] == [("i1", "v1"), ("i2", "v2"), ("i3", "v1")]
    assert [c[1]["pageToken"] for c in svc.calls] == [None, "p2"]
    assert quota.used(conn) == 2


def test_create_playlist(conn):
    client, svc, _ = make(conn, {"playlists.insert": [
        {"id": "PLnew", "snippet": {"title": "osu!"}, "status": {"privacyStatus": "private"}}]})
    p = client.create_playlist("osu!")
    assert (p.id, p.privacy) == ("PLnew", "private")
    assert svc.calls[0][1]["body"]["status"] == {"privacyStatus": "private"}
    with pytest.raises(ValueError):
        client.create_playlist("x", privacy="secret")


def test_check_videos_chunks_by_50(conn):
    ids = [f"v{i:010d}" for i in range(120)]

    def handler(kw):
        req = kw["id"].split(",")
        assert len(req) <= 50
        return {"items": [{"id": v, "status": {"privacyStatus": "public", "uploadStatus": "processed"}}
                          for v in req if v != ids[7]]}

    client, svc, _ = make(conn, {"videos.list": handler})
    result = client.check_videos(ids + [ids[0]])   # duplicates are ignored
    assert svc.count("videos.list") == 3
    assert result[ids[7]] == "deleted"
    assert sum(1 for a in result.values() if a == "ok") == 119


@pytest.mark.parametrize(
    "video, expected",
    [
        (None, "deleted"),
        ({"status": {"uploadStatus": "rejected"}}, "deleted"),
        ({"status": {"privacyStatus": "private"}}, "private"),
        ({"status": {"privacyStatus": "public"}, "contentDetails": {"regionRestriction": {"blocked": ["JP"]}}}, "region_blocked"),
        ({"status": {"privacyStatus": "public"}, "contentDetails": {"regionRestriction": {"allowed": ["US"]}}}, "region_blocked"),
        ({"status": {"privacyStatus": "public"}, "contentDetails": {"regionRestriction": {"allowed": ["JP"]}}}, "ok"),
        ({"status": {"privacyStatus": "unlisted"}}, "ok"),
        ({"status": {"privacyStatus": "public", "embeddable": False}}, "not_embeddable"),
    ],
)
def test_availability(video, expected):
    assert availability(video, "JP") == expected
