import pytest

from osu2yt import db
from osu2yt.matching import matcher
from osu2yt.matching.scoring import Song
from osu2yt.matching.search import SearchError, VideoInfo, parse_video_id

SONG = Song("Humanoid", "ヒューマノイド", "ZUTOMAYO", "ずっと真夜中でいいのに。", 259.9)


def info(vid, title, channel="someone", verified=False, duration=260.0):
    return VideoInfo(vid, title, channel, None, verified, duration)


GOOD = info("GAB26GgJ8V8", "ずっと真夜中でいいのに。『ヒューマノイド』MV", "ずっと真夜中でいいのに。 ZUTOMAYO", True)
COVER = info("FRMTbL9xa3M", "ヒューマノイド / ずっと真夜中でいいのに。【Bass Cover】", "やまもとひかる", True, 263)
OTHER = info("xxxxxxxxxxx", "unrelated video", duration=30)


@pytest.mark.parametrize(
    "text, vid",
    [
        ("GAB26GgJ8V8", "GAB26GgJ8V8"),
        ("https://www.youtube.com/watch?v=GAB26GgJ8V8", "GAB26GgJ8V8"),
        ("https://www.youtube.com/watch?list=PL1&v=GAB26GgJ8V8&t=3", "GAB26GgJ8V8"),
        ("https://youtu.be/GAB26GgJ8V8?si=abc", "GAB26GgJ8V8"),
        ("https://music.youtube.com/watch?v=GAB26GgJ8V8", "GAB26GgJ8V8"),
        ("https://www.youtube.com/shorts/GAB26GgJ8V8", "GAB26GgJ8V8"),
        ("https://www.youtube-nocookie.com/embed/GAB26GgJ8V8", "GAB26GgJ8V8"),
        ("not a video", None),
        ("", None),
    ],
)
def test_parse_video_id(text, vid):
    assert parse_video_id(text) == vid


class TestBuildQueries:
    def test_unicode_romaji_title(self):
        assert matcher.build_queries(SONG) == ["ずっと真夜中でいいのに。 ヒューマノイド", "ZUTOMAYO Humanoid", "ヒューマノイド"]

    def test_identical_unicode_is_not_repeated(self):
        s = Song("Birdsong", "Birdsong", "James Landino", "James Landino", 134)
        assert matcher.build_queries(s) == ["James Landino Birdsong", "Birdsong"]

    def test_tags_and_cv_removed(self):
        s = Song("Onegai Muscle (TV Size)", "お願いマッスル (TV Size)", "Hibiki (CV: Ai)", "紗倉ひびき (CV: ファイルーズあい)", 91)
        assert matcher.build_queries(s)[0] == "紗倉ひびき お願いマッスル"

    def test_subtitle_dashes_are_not_exclusion_operators(self):
        s = Song("lilac slumber -cosmic ver.-", "lilac slumber -cosmic ver.-", "Annabel", "Annabel", 255)
        assert matcher.build_queries(s)[0] == "Annabel lilac slumber cosmic ver."
        s = Song("Hoshizora no kanran -sha-", "Hoshizora no kanran -sha-", "AAAA", "ああああ", 136)
        assert all("-" not in q for q in matcher.build_queries(s))

    def test_osu_edit_tag_removed(self):
        s = Song("Goodbye Goodnight (feat. shully) (osu! edit)", "Goodbye Goodnight (feat. shully) (osu! edit)",
                 "rejection", "rejection", 111)
        assert matcher.build_queries(s)[0] == "rejection Goodbye Goodnight (feat. shully)"

    def test_katakana_query_for_romaji_only_title(self):
        s = Song("Arumagedon", "Arumagedon", "shinra-bansho", "shinra-bansho", 270)
        assert matcher.build_queries(s)[-1] == "shinra-bansho アルマゲドン"
        assert not any("ヒューマノイド" in q and q.isascii() for q in matcher.build_queries(SONG))
        english = Song("Lilac", "Lilac", "MiddleIsland", "MiddleIsland", 130)
        assert matcher.build_queries(english) == ["MiddleIsland Lilac", "Lilac"]


@pytest.mark.parametrize("raw, expected", [
    ("Annabel lilac slumber -cosmic ver.-", "Annabel lilac slumber cosmic ver."),
    ("ClariS ヒトリゴト ～TV MIX～", "ClariS ヒトリゴト TV MIX"),
    ('EmoCosine × BEMANI Sound Team "HuΣeR" KOTONOHA', "EmoCosine × BEMANI Sound Team HuΣeR KOTONOHA"),
    ("Mitsukiyo Gregorius | Symphony", "Mitsukiyo Gregorius Symphony"),
    ("shinra-bansho Re:TrymenT", "shinra-bansho Re:TrymenT"),   # in-word hyphens stay
    ("#hashtag +plus C#", "hashtag plus C#"),
    ("光-HIKARI-", "光-HIKARI"),
])
def test_youtube_query(raw, expected):
    assert matcher.youtube_query(raw) == expected


def test_zero_result_fallbacks():
    assert matcher.zero_result_fallbacks("Hatsune Miku no Shoushitsu") == [
        "Hatsune Miku no Shoushitsu song", "Hatsune Miku Shoushitsu"]
    assert matcher.zero_result_fallbacks("rerulili Nou Shou Sakuretsu Girl") == ["rerulili Nou Shou Sakuretsu Girl song"]


class FakeSearch:
    def __init__(self, results: dict[str, list[VideoInfo]]):
        self.results = results
        self.calls: list[str] = []

    def __call__(self, query, n):
        self.calls.append(query)
        return self.results.get(query, [])


class TestMatchSong:
    def test_stops_after_confident_match(self):
        fake = FakeSearch({"ずっと真夜中でいいのに。 ヒューマノイド": [COVER, GOOD]})
        o = matcher.match_song("o:1", SONG, search_fn=fake)
        assert fake.calls == ["ずっと真夜中でいいのに。 ヒューマノイド"]
        assert (o.decision.status, o.decision.video_id) == ("confirmed", GOOD.video_id)
        assert [c.info.video_id for c in o.candidates] == [GOOD.video_id, COVER.video_id]

    def test_falls_back_to_next_query_and_merges(self):
        fake = FakeSearch({"ずっと真夜中でいいのに。 ヒューマノイド": [OTHER], "ZUTOMAYO Humanoid": [OTHER, GOOD]})
        o = matcher.match_song("o:1", SONG, search_fn=fake)
        assert len(fake.calls) == 2
        assert o.decision.video_id == GOOD.video_id
        assert len(o.candidates) == 2  # OTHER not duplicated

    def test_nothing_found(self):
        o = matcher.match_song("o:1", SONG, search_fn=FakeSearch({}))
        assert (o.decision.status, o.decision.reason) == ("needs_review", "no_results")

    def test_zero_results_retry_with_song_suffix(self):
        romaji = Song("Hatsune Miku no Shoushitsu", "Hatsune Miku no Shoushitsu", "cosMo", "cosMo", 290)
        hit = info("HIT00000001", "Hatsune Miku no Shoushitsu (Full)", "cosMo@bousouP Official Channel", True, 290)
        fake = FakeSearch({"cosMo Hatsune Miku no Shoushitsu song": [hit]})
        o = matcher.match_song("o:1", romaji, search_fn=fake)
        assert fake.calls[:2] == ["cosMo Hatsune Miku no Shoushitsu", "cosMo Hatsune Miku no Shoushitsu song"]
        assert o.candidates[0].query == "cosMo Hatsune Miku no Shoushitsu song"
        assert o.decision.video_id == hit.video_id

    def test_chart_videos_sink_below_real_upload(self):
        chart = info("CHART000001", "ずっと真夜中でいいのに。 - ヒューマノイド [Insane] +HD 98.12%", "osu player", False, 260)
        fake = FakeSearch({"ずっと真夜中でいいのに。 ヒューマノイド": [chart, GOOD]})
        o = matcher.match_song("o:1", SONG, search_fn=fake)
        assert [c.info.video_id for c in o.candidates] == [GOOD.video_id, chart.video_id]
        assert o.decision.video_id == GOOD.video_id

    def test_search_error_is_reported(self):
        def boom(q, n):
            raise SearchError("network down")
        o = matcher.match_song("o:1", SONG, search_fn=boom)
        assert o.error and o.decision is None


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    c.execute(
        """INSERT INTO beatmapsets(key, title, title_unicode, artist, artist_unicode, duration_sec, is_pack, in_library,
                                   match_status, date_added)
           VALUES('o:1', 'Humanoid', 'ヒューマノイド', 'ZUTOMAYO', 'ずっと真夜中でいいのに。', 259.9, 0, 1, 'unmatched', '2025')"""
    )
    c.commit()
    yield c
    c.close()


def status(conn, key="o:1"):
    return conn.execute("SELECT match_status, chosen_video_id, chosen_by FROM beatmapsets WHERE key=?", (key,)).fetchone()[:]


class TestRunAndActions:
    def test_run_saves_candidates_and_decision(self, conn):
        fake = FakeSearch({"ずっと真夜中でいいのに。 ヒューマノイド": [GOOD, COVER]})
        s = matcher.run_matching(conn, matcher.select_rows(conn), workers=1, search_fn=fake)
        assert (s.confirmed, s.needs_review, s.failed) == (1, 0, 0)
        assert status(conn) == ("confirmed", GOOD.video_id, "auto")
        assert conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0] == 2
        assert matcher.select_rows(conn) == []  # nothing unmatched any more

    def test_failed_search_leaves_set_unmatched(self, conn):
        def boom(q, n):
            raise SearchError("x")
        s = matcher.run_matching(conn, matcher.select_rows(conn), workers=1, search_fn=boom)
        assert s.failed == 1
        assert status(conn)[0] == "unmatched"

    def test_choose_candidate_then_manual_then_skip(self, conn):
        matcher.run_matching(conn, matcher.select_rows(conn), workers=1,
                             search_fn=FakeSearch({"ずっと真夜中でいいのに。 ヒューマノイド": [GOOD, COVER]}))
        assert matcher.choose(conn, "o:1", f"https://youtu.be/{COVER.video_id}") == "user"
        assert status(conn) == ("confirmed", COVER.video_id, "user")

        manual = info("MANUAL00001", "ヒューマノイド (official audio)", "ずっと真夜中でいいのに。 - Topic")
        assert matcher.choose(conn, "o:1", manual.video_id, fetch_fn=lambda v: manual) == "manual"
        assert status(conn) == ("confirmed", manual.video_id, "manual")

        # re-matching keeps the manual candidate and the user's choice
        matcher.save_outcome(conn, matcher.match_song("o:1", SONG, search_fn=FakeSearch({})))
        assert conn.execute("SELECT COUNT(*) FROM candidates WHERE query='manual'").fetchone()[0] == 1
        assert status(conn) == ("confirmed", manual.video_id, "manual")

        matcher.skip(conn, "o:1")
        assert status(conn)[0] == "skipped"
        assert matcher.unskip(conn, "o:1") == "confirmed"

    def test_rematch_keeps_user_picked_candidate_row(self, conn):
        fake = FakeSearch({"ずっと真夜中でいいのに。 ヒューマノイド": [GOOD, COVER]})
        matcher.run_matching(conn, matcher.select_rows(conn), workers=1, search_fn=fake)
        matcher.choose(conn, "o:1", COVER.video_id)
        matcher.save_outcome(conn, matcher.match_song("o:1", SONG, search_fn=FakeSearch({})))
        assert status(conn) == ("confirmed", COVER.video_id, "user")
        ids = {r[0] for r in conn.execute("SELECT video_id FROM candidates")}
        assert ids == {COVER.video_id}

    def test_auto_choice_is_replaced_by_rematch(self, conn):
        matcher.run_matching(conn, matcher.select_rows(conn), workers=1,
                             search_fn=FakeSearch({"ずっと真夜中でいいのに。 ヒューマノイド": [GOOD]}))
        matcher.save_outcome(conn, matcher.match_song("o:1", SONG, search_fn=FakeSearch({})))
        assert status(conn) == ("needs_review", None, None)

    def test_user_choices_survive_needs_review_rematch(self, conn):
        conn.execute("UPDATE beatmapsets SET match_status='needs_review', chosen_video_id='x', chosen_by='user'")
        assert matcher.select_rows(conn, "needs_review") == []

    def test_choose_errors(self, conn):
        with pytest.raises(matcher.MatchActionError):
            matcher.choose(conn, "o:1", "not a url")
        with pytest.raises(matcher.MatchActionError):
            matcher.choose(conn, "o:1", "AAAAAAAAAAA", fetch_fn=lambda v: None)
        with pytest.raises(matcher.MatchActionError):
            matcher.choose(conn, "o:404", "AAAAAAAAAAA")

    def test_rescore_uses_stored_candidates(self, conn):
        matcher.run_matching(conn, matcher.select_rows(conn), workers=1,
                             search_fn=FakeSearch({"ずっと真夜中でいいのに。 ヒューマノイド": [GOOD, COVER]}))
        conn.execute("UPDATE beatmapsets SET match_status='needs_review', chosen_video_id=?", (COVER.video_id,))
        s = matcher.rescore(conn, matcher.select_rows(conn, "auto"))
        assert (s.total, s.confirmed) == (1, 1)
        assert status(conn) == ("confirmed", GOOD.video_id, "auto")

    def test_confirm_needs_review(self, conn):
        conn.execute("UPDATE beatmapsets SET match_status='needs_review', chosen_video_id='x', chosen_by='auto'")
        matcher.confirm(conn, "o:1")
        assert status(conn) == ("confirmed", "x", "user")
