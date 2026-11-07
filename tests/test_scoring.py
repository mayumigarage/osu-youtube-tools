import pytest

from osu2yt.matching.scoring import (
    AUTO_ACCEPT_SCORE,
    CHART_PENALTY,
    GAME_PENALTY,
    Candidate,
    Score,
    Song,
    artist_keys,
    artist_score,
    chart_signals,
    decide,
    duration_score,
    negative_words,
    official_score,
    score,
    title_score,
)

ZUTOMAYO = Song("Humanoid", "ヒューマノイド", "ZUTOMAYO", "ずっと真夜中でいいのに。", 259.9)
MUSCLE = Song(
    "Onegai Muscle (TV Size)", "お願いマッスル (TV Size)",
    "Hibiki Sakura (CV: Fairouz Ai) & Naruzo Machio (CV: Ishikawa Kaito)",
    "紗倉ひびき (CV: ファイルーズあい) & 街雄鳴造 (CV: 石川界人)", 91.0,
)


def cand(title, channel="someone", verified=False, duration=260.0, vid="aaaaaaaaaaa", live=False):
    return Candidate(vid, title, channel, verified, duration, live)


class TestDuration:
    @pytest.mark.parametrize("video, expected", [(260, 1.0), (262.9, 1.0), (269.9, pytest.approx(0.6)),
                                                 (279.9, pytest.approx(0.3)), (289.9, pytest.approx(0.0)),
                                                 (400, 0.0)])
    def test_normal_mode(self, video, expected):
        assert duration_score(259.9, video, full_size_mode=False) == expected

    def test_unknown_lengths(self):
        assert duration_score(259.9, None, False) == 0.3
        assert duration_score(None, 200, False) == 0.5

    def test_full_size_mode_prefers_full_length(self):
        tv = 91.0
        full = duration_score(tv, 250, full_size_mode=True)
        tv_video = duration_score(tv, 90, full_size_mode=True)
        assert full == 1.0
        assert tv_video == 0.4
        assert full > tv_video
        assert duration_score(tv, 60, True) == 0.0          # shorter than the osu! audio
        assert duration_score(tv, 1200, True) == 0.0        # hour-long loops etc.


def test_title_score_uses_unicode_and_romaji_and_ignores_tags():
    assert title_score(ZUTOMAYO, "ずっと真夜中でいいのに。『ヒューマノイド』MV") == 1.0
    assert title_score(ZUTOMAYO, "ZUTOMAYO - Humanoid (Official Video)") == 1.0
    assert title_score(MUSCLE, "TVアニメ「ダンベル何キロ持てる？」OP「お願いマッスル」") == 1.0
    assert title_score(ZUTOMAYO, "全く別の曲") == 0.0


def test_artist_keys_split_and_cv():
    whole, names = artist_keys("紗倉ひびき (CV: ファイルーズあい) & 街雄鳴造 (CV: 石川界人)")
    assert whole == "紗倉ひびき街雄鳴造"
    assert {"紗倉ひびき", "街雄鳴造", "ファイルーズあい", "石川界人"} <= set(names)
    _, names = artist_keys("Mitsukiyo, IOSYS, Kano")
    assert names == ["mitsukiyo", "iosys", "kano"]


class TestOfficial:
    def test_topic_channel(self):
        assert official_score(ZUTOMAYO, "ずっと真夜中でいいのに。 - Topic", False) == (1.0, "topic")

    def test_verified_artist_channel(self):
        assert official_score(ZUTOMAYO, "ずっと真夜中でいいのに。 ZUTOMAYO", True) == (1.0, "official_artist")

    def test_verified_other_channel(self):
        assert official_score(ZUTOMAYO, "Some Label", True) == (0.6, "verified")

    def test_unrelated(self):
        assert official_score(ZUTOMAYO, "random uploader", False) == (0.0, None)


class TestNegativeWords:
    def test_cover_and_live(self):
        assert "cover" in negative_words(ZUTOMAYO, "ヒューマノイド / ずっと真夜中でいいのに。【Bass Cover】")
        assert "live" in negative_words(ZUTOMAYO, "ヒューマノイド (LIVE 2023)")

    @pytest.mark.parametrize("title, word", [
        ("【JUMP MV】NARUTO x Humanoid (ZUTOMAYO)【Official Trailer】", "trailer"),
        ("【なすふみ×琴音】ヒューマノイド【ヲタ芸】", "ヲタ芸"),
        ("ずっと真夜中でいいのに。の『ヒューマノイド』の音程下げたら", "音程"),
    ])
    def test_trailers_dances_and_pitch_edits(self, title, word):
        assert word in negative_words(ZUTOMAYO, title)

    def test_word_boundaries(self):
        assert negative_words(ZUTOMAYO, "Alive and well - Humanoid") == []
        hits, _ = chart_signals(ZUTOMAYO, "Humanoid [Insane] osu! gameplay")
        assert {"gameplay", "difficulty"} <= set(hits)

    def test_word_in_osu_title_is_not_penalised(self):
        song = Song("Piano Song", "Piano Song", "A", "A", 100)
        assert negative_words(song, "A - Piano Song") == []


class TestTotal:
    def test_official_mv_scores_high(self):
        s = score(ZUTOMAYO, cand("ずっと真夜中でいいのに。『ヒューマノイド』MV", "ずっと真夜中でいいのに。 ZUTOMAYO", True, 260))
        assert s.total == 1.0
        assert s.total >= AUTO_ACCEPT_SCORE

    def test_cover_scores_below_threshold(self):
        s = score(ZUTOMAYO, cand("ヒューマノイド / ずっと真夜中でいいのに。【Bass Cover】", "やまもとひかる", True, 263))
        assert s.total < AUTO_ACCEPT_SCORE
        assert "penalty_words" in s.parts

    def test_tv_size_song_prefers_full_video(self):
        full = score(MUSCLE, cand("TVアニメ「ダンベル何キロ持てる？」OP お願いマッスル 紗倉ひびき 街雄鳴造 MV", duration=240))
        tv = score(MUSCLE, cand("お願いマッスル (TV Size) 紗倉ひびき 街雄鳴造", duration=90))
        assert full.total > tv.total
        assert "penalty_short" in tv.parts
        assert "full_size_mode" in full.notes

    def test_live_stream_penalised(self):
        live = score(ZUTOMAYO, cand("ずっと真夜中でいいのに。 ヒューマノイド", duration=None, live=True))
        assert live.parts["penalty_live"] < 0


def test_partial_title_needs_most_of_the_title():
    melt = Song("melt bitter (Cut Ver.)", "melt bitter (Cut Ver.)", "Seren Azuma", "東 雪蓮", 126)
    assert title_score(melt, "bitter sip") == 0.0
    yume = Song("Yume no Kissaten", "ユメの喫茶店", "Mitsukiyo", "ミツキヨ", 222)
    assert title_score(yume, "ユメのロースタリー") == 0.0
    kanran = Song("Hoshizora no kanran -sha-", "Hoshizora no kanran -sha-", "AAAA", "AAAA", 136)
    assert title_score(kanran, "AAAA - Hoshizora no kanran") >= 0.75


def test_plural_negative_word():
    assert "instrumental" in negative_words(ZUTOMAYO, "Humanoid (instrumentals)")


def test_osu_map_videos_penalised():
    hits, penalty = chart_signals(ZUTOMAYO, "ずっと真夜中でいいのに。 - ヒューマノイド [map by someone]")
    assert "map by" in hits and penalty == CHART_PENALTY


@pytest.mark.parametrize("title, hit", [
    ("CuttingEdgeInsaneArtifact - Marmalade butcher [⭐7,09 - 96,71%]", "star_rating"),
    ("Humanoid +HDDT 98.52% FC 412pp", "pp"),
    ("Humanoid 98.52% FC", "accuracy"),
    ("Humanoid HD DT", "mods"),
    ("[o!m7k] 6.25 ZUTOMAYO - Humanoid", "osu_mode_tag"),
    # seen in real search results
    ("lapix - Foolish Hero (FutureCore Mix) [Hard] +HD SS", "difficulty"),
    ("Canon (Cranky Remix) / J. Pachelbel Remixed by Cranky [CHAOS Lv.15]", "difficulty"),
    ("[SDVX] Daydream café -Euro Hopping Mix- (MXM)", "difficulty"),
    ("osu! / Hotaru Murasaki - Re:TrymenT [Cookiezi's Insane] +HD", "mapper_diff"),
    ("Yuta Imai - Into The Madness [CSP 17]", "level"),
    ("(charter: 并非安全) (in. 15) MYUKKE. - Anti Safety Zone", "level"),
    ("Vacant Rhapsody Re:Master Lv.15", "level"),
    ("osu!mania | Qiu Ri Wu Yu by Bu Jiang Li Mao (2.29*) | map by FallFish", "star_rating"),
    ("osu! Kingling — Asuka Ohta 6.3★ HDDT FC", "star_rating"),
    ("Hinatabi Bitter Sweets & Coconatsu - Chocolate Smile - 7K MX", "keys"),
    ("mocchie - Nyan Nyan Drive (D: [CRN-AR10-]", "osu_settings"),
    ("#3 on a CS6.5 map | At the Garret x Kekkan Orgel - Replica [Facsimile] Highest Non-Easy Combo!!", "osu_settings"),
    ("★3.03  Yuta Imai - Into The Madness  FC // S //  990,063", "score"),
    ("BTMC REACTS TO BADEU Lapix - Nexta [ANTIGRAVITY] + HR", "mods"),
    ("Yaong | NOMA - PEPSI * MAN [INHU MAN] HD SS LOVED #1", "grade"),
    ("[Osu!] Sakura Mirage (STARLiGHT Mix) DT FC 99%", "accuracy"),
    ("【SDVX VM】 KAC 2012 ULTIMATE MEDLEY [GRV~ADV] PUC (Hand Shot)", "hand shot"),
    ("[osu!mania 自製譜] ぺのれり - Vacant Rhapsody", "自製譜"),
    ("【譜面確認用】 ヒューマノイド MASTER", "譜面"),
    ("[최초 클리어] 후살 급발진 주의!! / ああああ - 星空の觀覽車 (Hoshizora no kanran-sha)", "클리어"),
    ("[ 완벽한 플레이 ] ああああ - 星空の観覧車 | by FluffyYoshi", "플레이"),
    # slipped through the first re-search
    ("Sota Fujimori - VOLTEXES III - S rank on tech map", "grade"),
    ("JO*STARS ~TOMMY, Coda, JIN~ - JoJo Sono Chi no Kioku~end of THE WORLD~ [STAR PLATINUM] 1xMiss Choke", "grade"),
    ("Fractal Dreamers - Fata Morgana [captin's Insane] 2 miss", "grade"),
    ("TOUHOU (SENYA) - THOUGH THE SCENT LINGERS, THE FLOWER SCATTERED catch the beat", "catch the beat"),
    ("senya   Hitomi ni Kakusareta Omoi N a s y a's Lunatic", "mapper_diff"),
    ("Ichinoryu Uehara - Mou Ichido Ano Sora o [The softness of a peach] {Wanpachi}", "mapper_diff"),
    ("Ceui - Hoshi Ori Yume Mirai [Star] mouse-ONLY", "mouse-only"),
    ("[Custom 12★] Staring at star - MisoilePunch♪ ～タケノコ添え～", "star_rating"),
    ("Playing「EmoCosine - MAGENTA POTION」in maimai", "played"),
])
def test_play_video_patterns(title, hit):
    hits, penalty = chart_signals(ZUTOMAYO, title)
    assert hit in hits
    assert penalty == CHART_PENALTY


@pytest.mark.parametrize("title", [
    "ずっと真夜中でいいのに。『ヒューマノイド』MV (HD)",
    "ずっと真夜中でいいのに。『ヒューマノイド』MV [4K]",
    "恋色マリアージュ OP - my sweet heart / 霜月はるか 4K60fps",
    "Humanoid (Remaster)",
    "Humanoid [Official Music Video]",
    "Humanoid (Hard Rock ver.)",
    "Humanoid 2023 Remastered",
    "Humanoid - Life's Hard",
    "Humanoid (It's Easy!)",
    "Humanoid - Miss You",
    "ZUTOMAYO - Humanoid [Monstercat Release]",
    "ZUTOMAYO - Humanoid [Dubstep]",
    "ZUTOMAYO - Humanoid [Lyrics]",
])
def test_play_video_patterns_ignore_normal_titles(title):
    assert chart_signals(ZUTOMAYO, title) == ([], 0.0)


def test_chart_words_in_osu_metadata_are_not_penalised():
    song = Song("★Shooting Star 2.0", "★Shooting Star 2.0", "A", "A", 100)
    assert "star_rating" not in chart_signals(song, "A - ★Shooting Star 2.0")[0]


class TestGameNames:
    SDVX_SONG = Song("Enigma", "Enigma", "Toromaru", "とろまる", 120,
                     source="SOUND VOLTEX III GRAVITY WARS", tags="SDVX Piano Konami Bemani")

    def test_game_name_alone_is_a_weaker_penalty(self):
        hits, penalty = chart_signals(ZUTOMAYO, "【オンゲキ】ヒューマノイド")
        assert hits == ["オンゲキ"] and penalty == GAME_PENALTY

    def test_game_named_in_source_or_tags_is_fine(self):
        assert chart_signals(self.SDVX_SONG, "MisoilePunch - Enigma (From: SOUND VOLTEX III)") == ([], 0.0)
        assert chart_signals(self.SDVX_SONG, "[SDVX III] Toromaru - Enigma") == ([], 0.0)

    def test_chart_signals_still_count_for_game_songs(self):
        hits, penalty = chart_signals(self.SDVX_SONG, "[SDVX III] Toromaru - Enigma [EXH 17] PUC")
        assert penalty == CHART_PENALTY and "difficulty" in hits

    def test_osu_original_uploaded_by_artist(self):
        song = Song("Rift Walker", "Rift Walker", "Kry.exe", "Kry.exe", 172, source="Locus 2025")
        assert chart_signals(song, "[osu! Locus 2025] Kry.exe - Rift Walker", "Kry.exe", artist_upload=True) == ([], 0.0)
        assert chart_signals(song, "[osu! Locus 2025] Kry.exe - Rift Walker", "someone")[0] == ["osu"]

    def test_osu_in_source_exempts_osu_word(self):
        song = Song("Anti Safety Zone", "Anti Safety Zone", "MYUKKE.", "MYUKKE.", 147,
                    source="osu!catch World Cup 2023")
        assert chart_signals(song, "MYUKKE. - Anti Safety Zone (from osu!catch World Cup 2023)") == ([], 0.0)

    def test_osu_tag_does_not_exempt(self):
        song = Song("X", "X", "A", "A", 100, tags="osu! mania 7k")
        assert chart_signals(song, "A - X (osu! gameplay)")[0]

    def test_channel_gives_play_video_away(self):
        assert chart_signals(ZUTOMAYO, "ヒューマノイド", "Jane plays Osu!") == (["channel:osu"], GAME_PENALTY)
        assert chart_signals(ZUTOMAYO, "AAAA - Humanoid", "PsyHold [Phi Fanmade]") == (["channel:fanmade"], CHART_PENALTY)
        assert chart_signals(ZUTOMAYO, "Humanoid", "Cookiezi Replays") == (["channel:replay"], CHART_PENALTY)

    def test_game_audio_rips_are_fine(self):
        assert chart_signals(ZUTOMAYO, "[Lanota] Humanoid - ZUTOMAYO【音源】 【高音質】") == ([], 0.0)
        assert chart_signals(ZUTOMAYO, "【GITADORA EXCHAIN GST】 Humanoid") == ([], 0.0)
        assert chart_signals(ZUTOMAYO, "【BGA】ヒューマノイド【jubeat】") == ([], 0.0)
        # ... but not when the video is clearly a play video
        assert chart_signals(ZUTOMAYO, "[Lanota] Humanoid【音源】 [Master] AP")[1] == CHART_PENALTY

    def test_artist_upload_may_use_ambiguous_brackets(self):
        assert chart_signals(ZUTOMAYO, "sl4 Humanoid[ULTIMATE]", artist_upload=True) == ([], 0.0)
        assert chart_signals(ZUTOMAYO, "[Phigros/2023 April Fool's Day] Humanoid", artist_upload=True) == ([], 0.0)
        assert chart_signals(ZUTOMAYO, "Humanoid ★11~13", artist_upload=True)[0] == ["star_rating"]

    def test_playing_in_a_game_counts_even_for_the_artist(self):
        title = "Playing「ZUTOMAYO - Humanoid」in maimai"
        assert chart_signals(ZUTOMAYO, title, "ZUTOMAYO", artist_upload=True) == (["played"], CHART_PENALTY)

    def test_osu_metadata_line_with_difficulty_bracket(self):
        assert chart_signals(ZUTOMAYO, "ZUTOMAYO - Humanoid [Saikyou!!]") == (["osu_format"], GAME_PENALTY)
        assert chart_signals(ZUTOMAYO, "ZUTOMAYO - Humanoid [Saikyou!!]", "ZUTOMAYO", artist_upload=True) == ([], 0.0)
        song = Song("what's done is done", "what's done is done", "Tanza3D", "Tanza3D", 100)
        assert chart_signals(song, "Tanza3D - what's done is done [what's done is done]") == ([], 0.0)

    def test_osu_metadata_line_tolerates_spacing(self):
        song = Song("Peko Miko Daisensou!!", "ぺこみこ大戦争！！", "REDALiCE feat. Usada Pekora & Sakura Miko",
                    "REDALiCE feat. 兎田ぺこら & さくらみこ", 200)
        title = "REDALiCE feat. Usada Pekora & Sakura Miko  - Pekomiko Daisensou!! [Saikyou!!]"
        assert chart_signals(song, title) == (["osu_format"], GAME_PENALTY)

    @pytest.mark.parametrize("title", [
        "TVアニメ「ヒューマノイド」OP映像 ( ずっと真夜中でいいのに。／ヒューマノイド ）【毎日投稿企画】",
        "【ZUTOMAYO】Humanoid【中、日、羅字幕】lyrics",
        "【歌詞】ZUTOMAYO／Humanoid【フル】",
        "ZUTOMAYO「Humanoid」【試聴】",
        "ZUTOMAYO - Humanoid [NOFX]",
    ])
    def test_osu_metadata_line_leaves_music_uploads_alone(self, title):
        assert chart_signals(ZUTOMAYO, title) == ([], 0.0)

    def test_osu_edit_song_still_penalises_osu_videos(self):
        song = Song("Goodbye Goodnight (osu! edit)", "Goodbye Goodnight (osu! edit)", "rejection", "rejection", 111)
        assert song.short_version
        assert chart_signals(song, "rejection - Goodbye Goodnight [osu!]") == (["osu"], GAME_PENALTY)


def test_chart_video_can_never_be_auto_accepted():
    perfect_but_chart = cand("ずっと真夜中でいいのに。『ヒューマノイド』[Insane] 98.5%",
                             "ずっと真夜中でいいのに。 ZUTOMAYO", True, 260)
    s = score(ZUTOMAYO, perfect_but_chart)
    assert s.total < AUTO_ACCEPT_SCORE
    assert any(n.startswith("chart:") for n in s.notes)
    assert s.parts["penalty_chart"] == -CHART_PENALTY


class TestLooseTitles:
    def test_symbol_replaced_by_letter(self):
        song = Song("Gregorius | Symphony", "Gregorius | Symphony", "Mitsukiyo", "ミツキヨ", 267)
        assert title_score(song, "Gregorius I Symphony") == 1.0
        assert title_score(song, "Gregorius Symphony") == 1.0
        assert title_score(song, "Gregorius and the Symphony") < 1.0

    def test_feat_tail_and_symbols(self):
        song = Song("Falling Down feat. Renko x TRIANGLE", "Falling Down feat. Renko × TRI△NGLE", "Zekk", "Zekk", 217)
        assert title_score(song, "Falling Down (feat. Renko & TRIANGLE)") == 1.0

    def test_accents(self):
        song = Song("asphyxia", "asphyxia", "Co shu Nie", "Cö shu Nie", 90)
        assert artist_score(song, "Co shu Nie - asphyxia", "x") == 1.0
        assert artist_score(song, "Cö shu Nie – asphyxia", "x") == 1.0


class TestRomaji:
    def test_kana_video_title_matches_romaji_only_metadata(self):
        song = Song("Arumagedon", "Arumagedon", "shinra-bansho", "shinra-bansho", 270)
        assert title_score(song, "あるまげどん") == 1.0
        assert title_score(song, "アルマゲドン / 森羅万象") == 1.0
        assert title_score(song, "Armageddon - Aerosmith") == 0.0

    def test_romaji_spelling_variants(self):
        song = Song("Hatsune Miku no Shoushitsu", "初音ミクの消失", "cosMo", "cosMo@暴走P", 290)
        assert title_score(song, "Hatsune Miku no Shōshitsu") == 1.0
        assert title_score(song, "Hatsune Miku no Shoshitsu (full)") == 1.0
        assert title_score(song, "Hoshizora no Kanransha") == 0.0

    def test_romaji_artist_vs_kana_channel(self):
        song = Song("Nou Shou Sakuretsu Girl", "Nou Shou Sakuretsu Girl", "rerulili", "rerulili", 191)
        assert artist_score(song, "脳漿炸裂ガール", "れるりり") == 1.0

    def test_short_names_do_not_collapse(self):
        song = Song("Ending", "Ending", "AAAA", "ああああ", 179)
        assert artist_score(song, "Happy Ending | Episode 10 Finale", "Lovebreak_Insights") == 0.0
        assert artist_score(song, "ああああ - Ending", "x") == 1.0

    def test_english_titles_do_not_use_romaji_rules(self):
        song = Song("Shot", "Shot", "A", "A", 100)
        assert not song.romanised_title
        assert title_score(song, "Shout") < 1.0


def sc(total, title=1.0, artist=1.0, official=0.0):
    return Score(total, {"title": title, "artist": artist, "official": official})


class TestDecide:
    def test_no_results(self):
        d = decide([])
        assert (d.status, d.reason, d.video_id) == ("needs_review", "no_results", None)

    def test_pack_always_review(self):
        assert decide([("a", sc(1.0), 100)], is_pack=True).reason == "pack"

    def test_low_score(self):
        assert decide([("a", sc(0.6), 100)]).reason == "low_score"

    def test_clear_winner(self):
        d = decide([("a", sc(0.8), 100), ("b", sc(0.5), 100)])
        assert (d.status, d.video_id) == ("confirmed", "a")

    def test_title_must_match(self):  # length + artist alone must not carry a match
        assert decide([("a", sc(0.75, title=0.0), 100)]).reason == "title_mismatch"
        # partial titles ("Lawn Wake IV" vs "Lawn Wake IX") are left for review
        assert decide([("a", sc(0.89, title=0.9), 100)]).reason == "title_mismatch"

    def test_artist_or_artist_channel_required(self):
        assert decide([("a", sc(0.72, artist=0.0, official=0.6), 100)]).reason == "artist_mismatch"
        assert decide([("a", sc(0.8, artist=0.0, official=1.0), 100)]).status == "confirmed"

    def test_close_runner_up_different_recording(self):
        assert decide([("a", sc(0.8), 100), ("b", sc(0.78), 180)]).reason == "close_second"

    def test_close_runner_up_same_recording_is_fine(self):
        assert decide([("a", sc(0.8), 113), ("b", sc(0.8), 116), ("c", sc(0.6), 300)]).status == "confirmed"

    def test_sure_score_ignores_runner_up(self):
        assert decide([("a", sc(0.9), 100), ("b", sc(0.89), 300)]).status == "confirmed"


def test_subtitle_only_match_gets_partial_title_credit():
    from osu2yt.matching.scoring import SUBTITLE_CREDIT, TITLE_REQUIRED, title_score
    song = Song(title="Elif to Shiro Kura no Yoru -Called-", title_unicode="エリフィと白昏の夜 -Called-",
                artist="Se-U-Ra", artist_unicode="せゆーら", duration_sec=120)
    assert title_score(song, "せゆーら - エリフィと白昏の夜 -Called-") == 1.0
    assert title_score(song, "エリフィと白昏の夜 / せゆーら") == SUBTITLE_CREDIT < TITLE_REQUIRED
    assert title_score(song, "全然違う曲") == 0.0
