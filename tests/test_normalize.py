import pytest

from osu2yt.library.normalize import (
    audio_stem,
    compact,
    is_pack,
    kana_to_romaji,
    looks_romaji,
    normalize_text,
    primary_variant,
    romaji_key,
    romaji_to_katakana,
    strip_brackets,
    strip_feat,
    strip_version_tags,
    unicode_or,
    version_tags,
)


@pytest.mark.parametrize(
    "src, expected",
    [
        ("Ｆｕｌｌ　Ｗｉｄｔｈ", "full width"),
        ("  A   B  ", "a b"),
        ("Artist ft. Someone", "artist feat. someone"),
        ("Artist featuring Someone", "artist feat. someone"),
        ("Artist Feat Someone", "artist feat. someone"),
        ("ｶﾀｶﾅ", "カタカナ"),
        (None, ""),
    ],
)
def test_normalize_text(src, expected):
    assert normalize_text(src) == expected


def test_compact_removes_symbols_keeps_cjk():
    assert compact("ずっと真夜中でいいのに。") == "ずっと真夜中でいいのに"
    assert compact("光-HIKARI- (TV Size)") == "光hikaritvsize"
    assert compact("Bwa !") == "bwa"


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Onegai Muscle (TV Size)", "Onegai Muscle"),
        ("お願いマッスル (TV Size)", "お願いマッスル"),
        ("光-HIKARI- (TV Size)", "光-HIKARI-"),
        ("Colorful (Cut Ver.)", "Colorful"),
        ("Song [Short Ver.]", "Song"),
        ("Song -TV Ver.-", "Song"),
        ("Song (TVサイズ)", "Song"),
        ("Song (Game Ver.)", "Song"),
        ("Birdsong", "Birdsong"),
        ("Miracle Sugite Yabai (feat. shully)", "Miracle Sugite Yabai (feat. shully)"),
    ],
)
def test_strip_version_tags(title, expected):
    assert strip_version_tags(title) == expected


@pytest.mark.parametrize(
    "title, tags",
    [
        ("Onegai Muscle (TV Size)", {"tv_size"}),
        ("Colorful (Cut Ver.)", {"cut"}),
        ("Song (Short Version)", {"short"}),
        ("Song (TVサイズ)", {"tv_size"}),
        ("Birdsong", set()),
        ("Television", set()),
    ],
)
def test_version_tags(title, tags):
    assert version_tags(title) == tags


def test_strip_brackets():
    assert strip_brackets("Title (feat. X) [Extended Mix]") == "Title"
    assert strip_brackets("曲名【MV】") == "曲名"
    assert strip_brackets("A (b (c))") == "A"


@pytest.mark.parametrize("title, tags", [
    ("Goodbye Goodnight (osu! edit)", {"osu_edit"}),
    ("Hitorigoto -TV MIX-", {"tv_size"}),
    ("Liar Mask anime size", {"tv_size"}),
    ("Endless Starlight (OP ver.)", {"tv_size"}),
    ("Song (Game Size)", {"game"}),
    ("Remixed ver.", set()),
])
def test_more_version_tags(title, tags):
    assert version_tags(title) == tags


def test_anime_size_ver_fully_stripped():
    assert strip_version_tags("syncretism animesize ver.") == "syncretism"


@pytest.mark.parametrize("kana, romaji", [
    ("アルマゲドン", "arumagedon"),
    ("あるまげどん", "arumagedon"),
    ("ダーリンダンス", "daarindansu"),
    ("ちょっと", "chotto"),
    ("マッチャ", "matcha"),
    ("ファイルーズ", "fairuuzu"),
    ("シェイク", "sheiku"),
    ("初音ミクの消失", "初音mikuno消失"),
])
def test_kana_to_romaji(kana, romaji):
    assert kana_to_romaji(kana) == romaji


def test_romaji_key_unifies_spellings():
    assert romaji_key("Toukyou") == romaji_key("Tōkyō") == romaji_key("Tokyo") == romaji_key("トウキョウ")
    assert romaji_key("rerulili") == romaji_key("れるりり")
    assert romaji_key("Shimbun") == romaji_key("Shinbun")
    assert romaji_key("Kimi wo") == romaji_key("Kimi o")


def test_romaji_detection_and_katakana():
    assert looks_romaji("Hoshizora no kanran -sha-")
    assert looks_romaji("Arumagedon")
    assert not looks_romaji("Nou Shou Sakuretsu Girl")
    assert not looks_romaji("Lilac")
    assert not looks_romaji("")
    assert romaji_to_katakana("Arumagedon") == "アルマゲドン"
    assert romaji_to_katakana("Konnichiwa 2") == "コンニチワ 2"


def test_fold_accents_keeps_kana():
    assert compact("Cö shu Nie") == "coshunie"
    assert compact("Daydream café") == "daydreamcafe"
    assert compact("ガギグ") == "ガギグ"


def test_strip_feat():
    assert strip_feat("Falling Down feat. Renko × TRI△NGLE") == "Falling Down"
    assert strip_feat("Song (feat. X)") == "Song"
    assert strip_feat("Song") == ""


def test_unicode_or():
    assert unicode_or("お願い", "Onegai") == "お願い"
    assert unicode_or("  ", "Onegai") == "Onegai"
    assert unicode_or(None, None) == ""


@pytest.mark.parametrize(
    "name, stem",
    [
        ("audio.mp3", "audio"),
        ("audio110.mp3", "audio"),
        ("audio 1.100x.mp3", "audio"),
        ("PV120.mp3", "pv"),
        ("spedup.mp3", ""),
        ("Coffee Cats.mp3", "coffeecats"),
        (None, ""),
    ],
)
def test_audio_stem(name, stem):
    assert audio_stem(name) == stem


def v(title, audio, diffs=1, length=100_000):
    return {"title": title, "audioFile": audio, "difficultyCount": diffs, "maxLengthMs": length}


class TestIsPack:
    def test_single_variant(self):
        assert not is_pack([v("Song", "audio.mp3")])

    def test_rate_edits_are_not_pack(self):  # e.g. HAG - Colorful (Cut Ver.)
        assert not is_pack([v("Colorful (Cut Ver.)", f"audio{n}.mp3") for n in ("", "110", "120", "130", "140")])
        assert not is_pack([v("FOCUS", a) for a in ("audio.mp3", "audio 1.100x.mp3", "audio 1.200x.mp3")])

    def test_spedup_pair_is_not_pack(self):
        assert not is_pack([v("YAJU&U x MEGALOVANIA", "audio.mp3"), v("YAJU&U x MEGALOVANIA", "spedup.mp3")])

    def test_title_suffix_variant_is_not_pack(self):  # "Hoshizora no kanran" / "... -sha-"
        assert not is_pack([v("Hoshizora no kanran", "audio.mp3"), v("Hoshizora no kanran -sha-", "audio.mp3")])

    def test_ost_pack_with_shared_title(self):  # e.g. Blue Archive OST Volume I
        names = ["Aira.mp3", "Coffee Cats.mp3", "Honey Jam.mp3", "Party Time.mp3"]
        assert is_pack([v("Blue Archive OST Volume I", n) for n in names])

    def test_two_different_titles(self):
        assert is_pack([v("Song A", "a.mp3"), v("Completely Different", "b.mp3")])


def test_primary_variant_prefers_difficulties_then_length():
    rate = v("Colorful", "audio110.mp3", diffs=1, length=90_000)
    orig = v("Colorful", "audio.mp3", diffs=1, length=100_000)
    assert primary_variant([rate, orig]) is orig
    many = v("Colorful", "audio120.mp3", diffs=4, length=80_000)
    assert primary_variant([rate, orig, many]) is many
    assert primary_variant([]) is None


def test_strip_subtitle():
    from osu2yt.library.normalize import strip_subtitle
    assert strip_subtitle("エリフィと白昏の夜 -Called-") == "エリフィと白昏の夜"
    assert strip_subtitle("FIN4LE ～終止符の彼方へ～") == "FIN4LE"
    assert strip_subtitle("lilac slumber -cosmic ver.-") == "lilac slumber"
    assert strip_subtitle("Re:Zero -Starting Life") == ""      # unclosed: not a subtitle
    assert strip_subtitle("A-B-C") == ""                        # no space before the delimiter
    assert strip_subtitle("Plain title") == ""
