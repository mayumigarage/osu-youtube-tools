"""Candidate scoring. Pure functions: no I/O, fully unit tested."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from ..library.normalize import (
    SHORT_VERSION_TAGS,
    compact,
    has_japanese,
    looks_romaji,
    normalize_text,
    romaji_key,
    strip_brackets,
    strip_feat,
    strip_subtitle,
    strip_version_tags,
    version_tags,
)

WEIGHTS = {"duration": 0.35, "title": 0.25, "artist": 0.20, "official": 0.20}

AUTO_ACCEPT_SCORE = 0.70   # top candidate must reach this ...
AUTO_ACCEPT_MARGIN = 0.08  # ... and beat the runner-up by this much
SURE_SCORE = 0.85          # above this, a close runner-up does not matter

PARTIAL_MIN = 0.75         # share of the title that must appear contiguously for partial credit
ROMAJI_MIN_LEN = 4         # romaji-variant matching only for keys at least this long
SUBTITLE_CREDIT = 0.85     # title matched only without its "-subtitle-" (never enough to auto-accept)
TITLE_REQUIRED = 1.0       # auto-accept needs the whole title in the video title ...
ARTIST_REQUIRED = 0.5      # ... and the artist in title/channel (or an artist-official channel)
OFFICIAL_ARTIST = 0.8      # official_score at/above this means the channel belongs to the artist

NEGATIVE_PENALTY = 0.30
SHORT_VIDEO_PENALTY = 0.15  # full-size mode: video itself is a TV size / short edit
CHART_PENALTY = 0.45        # rhythm-game play / chart video: 1.0 - 0.45 can never reach AUTO_ACCEPT_SCORE
GAME_PENALTY = 0.30         # only a rhythm-game name in the title (could still be a plain audio rip)

# Words that indicate "not the original recording" unless the osu! title itself contains them.
NEGATIVE_WORDS = [
    "cover", "カバー", "歌ってみた", "弾いてみた", "叩いてみた", "踊ってみた", "演奏してみた",
    "nightcore", "sped up", "speed up", "slowed", "reverb", "8bit", "8-bit", "music box", "オルゴール",
    "耐久", "1時間", "1 hour", "10 hours", "live", "ライブ", "reaction", "react", "reacts", "remix", "piano", "ピアノ",
    "dj", "tiktok", "viral",
    "instrumental", "off vocal", "karaoke", "カラオケ",
    "tutorial", "guitar", "ギター", "drum", "ドラム", "bass", "ベース", "fanmade", "fan made", "amv", "mad",
    "bootleg", "mashup", "arrange", "アレンジ",
    "trailer", "teaser", "予告", "ヲタ芸", "オタ芸", "wotagei", "音程", "ピッチ", "pitch",
]
_ASCII_WORD = re.compile(r"^[a-z0-9 !\-]+$")

# --- rhythm-game play / chart-preview videos ---------------------------------------------------
# Strong signals: the video is someone playing or previewing a chart. Ignored only when the osu!
# title / artist / source itself contains them.
CHART_WORDS = [
    "譜面", "手元", "プレイ動画", "直撮り", "外部出力", "自作譜面", "創作譜面", "自製譜", "フルコン", "全良", "理論値",
    "オートプレイ", "clap", "クラップ", "full combo", "all perfect", "all justice", "autoplay", "auto play",
    "map by", "mapped by", "mapper", "mapset", "beatmap", "mapping", "map release", "storyboard",
    "charter", "chart by", "charted by", "chart preview", "custom chart", "fanmade chart",
    "gameplay", "game play", "played by", "replay", "nomod", "hand shot", "handcam", "combo", "insane",
    "liveplay", "choke", "choked", "catch the beat", "ctb", "mouse only", "mouse-only", "tablet only",
    "keyboard only", "tech map", "map showcase",
    # Chinese / Korean play videos: 谱面 (chart), 自制谱, 全连 (FC), 클리어 (clear), 플레이 (play), 채보 (chart)
    "谱面", "制谱", "全连", "手元", "클리어", "플레이", "채보", "커스텀",
]
_DIFFICULTY = (
    r"easy|normal|hard|hyper|insane|expert|extra|extreme|master|another|lunatic|inferno|maximum|exhaust|"
    r"novice|advanced|heavenly|gravity|vivid|exceed|re:master|basic|beginner|challenge|oni|ura|"
    r"chaos|glitch|future|beyond|eternal|nov|adv|exh|mxm|inf|grv|hvn|vvd|xcd|ftr|byd|etr|bsc|mas"
)
# osu! difficulty names that follow a guest mapper's name without brackets ("Nasya's Lunatic")
_OSU_DIFF = r"easy|normal|hard|hyper|insane|expert|extra|extreme|lunatic|another|advanced|oni|overdose|kantan|futsuu|muzukashii"
CHART_PATTERNS = {
    # "⭐7,09" / "★6.5" / "6.3★" / "(2.29*)" / "★11~13" / "[Custom 12★]"
    "star_rating": re.compile(r"[⭐★☆]\s*\d+(?:[.,]\d+)?|\d+(?:[.,]\d+)?\s*[⭐★☆]|\d+[.,]\d+\s*\*"),
    "pp": re.compile(r"\b\d{2,4}(?:[.,]\d+)?\s*pp\b"),
    "accuracy": re.compile(r"(?<![\d.,])(?:\d{2}[.,]\d{1,2}|9\d|100)\s*%"),
    "osu_mode_tag": re.compile(r"(?<![a-z0-9])o!(?:m|std|ctb|taiko|mania)\d*k?(?![a-z])"),
    "mods": re.compile(
        r"(?<![a-z0-9])\+?(?:hd|dt|hr|nc|fl|ez)(?:\s*\+?\s*(?:hd|dt|hr|nc|fl|ez))+(?![a-z0-9])"
        r"|(?<![a-z0-9])\+\s*(?:hd|dt|hr|nc|fl|ez|ht|nf|so|td)(?![a-z0-9])"),
    # "[Insane]", "(MXM)", "[CHAOS Lv.15]", "[FTR 9]", "【EXPERT】"
    "difficulty": re.compile(
        rf"[\[\(【]\s*(?:{_DIFFICULTY})(?:\s*[~/\-]\s*(?:{_DIFFICULTY}))*\s*(?:lv\.?\s*)?(?:\d+\+?|\?+)?\s*[\]\)】]"),
    # osu! guest difficulty "[pkhg's Insane]" / "... Nasya's Lunatic" / "[Pure] {Amats}"
    "mapper_diff": re.compile(
        r"\[[^\]]*['’]s [^\]]*\]"
        r"|(?<![a-z0-9])(?!(?:it|that|what|there|here|he|she|who|life|let)['’])[a-z0-9_.\-]+['’]s\s+"
        rf"(?:{_OSU_DIFF})\+?(?=\s*(?:$|[^a-z\s]|(?:hd|dt|hr|nc|fl|ez|fc|ss)(?![a-z])))"
        r"|[\]】]\s*\{[^}]{2,30}\}"),
    "level": re.compile(r"(?<![a-z])(?:lv|lvl|level)\.?\s*(?:\d|\?)|(?<![a-z0-9])(?:csp|in|at)\.?\s*\d{1,2}\+?(?=\)|\])"
                        r"|(?<![a-z0-9])in\.\s*\d|(?<![a-z0-9])(?:csp|ftr|byd|etr|mxm|exh|grv|hvn|vvd|xcd)\s*\d"),
    "grade": re.compile(r"(?<![a-z0-9])(?:ss|puc|uc|ap|aj|ajc|pfc|mfc|apfc|fc|sdcb|\d+\s*(?:x\s*)?-?\s*miss(?:es)?)(?![a-z0-9])"
                        r"|(?<![a-z0-9])s{1,3}\+?\s*-?\s*rank(?![a-z])"),
    "keys": re.compile(r"(?<![a-z0-9])(?:[5679]|1[0-8])k(?![a-z0-9])"),  # not 4K/8K: video resolution
    "osu_settings": re.compile(r"(?<![a-z0-9])(?:ar|cs|od)\s*\d{1,2}(?:[.,]\d)?(?![a-z0-9])"),
    "score": re.compile(r"(?<![\d,.])\d{3},\d{3}(?![\d,])|\b\d{6,7}\s*score\b"),
}
# Weaker signal: a rhythm-game name. Audio rips are often labelled with the game too, so these are
# also ignored when the game appears in the osu! tags, or when the artist uploaded the video.
GAME_WORDS = [
    "osu", "mania", "taiko", "sdvx", "sound voltex", "ボルテ", "iidx", "beatmania", "bms", "pms", "maimai",
    "chunithm", "チュウニズム", "オンゲキ", "ongeki", "jubeat", "pop'n", "ポップン", "太鼓の達人", "groove coaster",
    "グルコス", "ノスタルジア", "nostalgia op", "nostalgia forte", "museca", "gitadora", "ddr",
    "dance dance revolution", "wacca", "arcaea", "phigros", "cytus", "deemo", "lanota", "dynamix", "rotaeno",
    "muse dash", "beat saber", "beatsaber", "adofai", "얼불춤", "rhythm doctor", "djmax", "ez2on", "o2jam",
    "stepmania", "etterna", "quaver", "malody", "notitg", "beatblock", "project diva", "プロセカ", "project sekai",
    "ユメステ", "d4dj", "デレステ", "ミリシタ", "rizline", "milthm", "orzmic", "paradigm reboot", "音ゲー", "rhythm game",
    "dtxmania", "dtx", "drummania", "guitarfreaks", "clone hero",
]
# A game name plus "playing" / "played" is a play video even on the artist's own channel
# ("Playing「EmoCosine - MAGENTA POTION」in maimai").
PLAY_WORDS = ["play", "playing", "played", "playthrough", "プレイ"]
# "Artist - Title [Something]" is the osu! metadata line as play-video uploaders paste it, with the
# difficulty name in brackets. Labels write "[Monstercat Release]" / "[Dubstep]" the same way, so it
# is only a weak signal and bracket words like these let it pass.
BRACKET_MUSIC_WORDS = [
    "release", "visualizer", "visualiser", "lyric", "lyrics", "audio", "hq", "hd", "1080p", "4k", "extended",
    "ep", "lp", "album", "single", "ost", "official", "mv", "pv", "music video", "video", "sub", "subs",
    "subbed", "vietsub", "subtitles", "歌詞", "가사", "free download", "download", "out now", "premiere", "full",
    "ver", "version", "mix", "remix", "edit", "remaster", "remastered", "original", "instrumental", "cover",
    "live", "nightcore", "dubstep", "drum & bass", "drum and bass", "dnb", "d&b", "trap", "house", "electro",
    "electronic", "hardstyle", "hardcore", "artcore", "future bass", "futurecore", "melodic", "hands up",
    "trance", "techno", "chill", "lofi", "lo-fi", "j-core", "speedcore", "breakcore", "glitch hop",
    "complextro", "drumstep", "eurobeat", "pop", "rock", "metal", "jazz", "funk", "disco", "synthwave",
    "vocaloid", "utau", "vo", "vocal", "short", "字幕", "フル", "ショート", "試聴",
]
# ... and a video that calls itself a lyric / official video is a music upload whatever its brackets say
MUSIC_UPLOAD_WORDS = ["lyric", "lyrics", "official", "mv", "pv", "music video", "歌詞", "字幕", "sub", "subs",
                      "vietsub", "audio", "release", "visualizer", "試聴"]
_TAG_EXEMPT_EXCLUDED = {"osu"}  # many maps tag themselves "osu!"; only title/artist/source count
# "【音源】" / "GST" / "BGA": a game's own audio or background video, not someone playing it.
AUDIO_WORDS = ["音源", "高音質", "audio", "ost", "gst", "soundtrack", "サウンドトラック", "サントラ", "bga", "nofx", "no fx"]
# Uploader names: "Cookiezi Replays", "[Phi Fanmade]" (strong); "Jane plays Osu!", "Osu!Music" (weak)
CHANNEL_WORDS = ["replay", "fanmade", "譜面", "手元", "gameplay"]
CHANNEL_GAME_WORDS = ["osu"]
# Patterns an artist's own upload may legitimately contain ("[ULTIMATE]", "[April Fool's Day]")
_ARTIST_SAFE_PATTERNS = {"difficulty", "mapper_diff", "level", "keys", "osu_settings", "score"}

# official_score kinds meaning the video comes from the artist themselves
ARTIST_UPLOAD_KINDS = {"topic", "official_artist", "artist_channel"}

_ARTIST_SPLIT_RE = re.compile(r"\s*(?:&|＆|,|、|/|×|\bx\b|\bvs\.?\b|\bfeat\.|\band\b|\bwith\b)\s*", re.IGNORECASE)
_CV_RE = re.compile(r"[\(（]\s*(?:cv|vo|vocal)[\.:：]?\s*([^\)）]+)[\)）]", re.IGNORECASE)


@dataclass
class Song:
    """What we know about the osu! side."""
    title: str
    title_unicode: str
    artist: str
    artist_unicode: str
    duration_sec: float | None
    source: str = ""
    tags: str = ""

    @property
    def short_version(self) -> bool:
        return bool((version_tags(self.title) | version_tags(self.title_unicode)) & SHORT_VERSION_TAGS)

    @property
    def romanised_title(self) -> bool:
        """The romanised title is a transliteration (so romaji spelling variants are worth trying)."""
        return has_japanese(self.title_unicode) or looks_romaji(self.title)

    @property
    def romanised_artist(self) -> bool:
        return has_japanese(self.artist_unicode) or looks_romaji(self.artist)


@dataclass
class Candidate:
    video_id: str
    title: str
    channel: str
    channel_verified: bool
    duration_sec: float | None
    live: bool = False


@dataclass
class Score:
    total: float
    parts: dict[str, float] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# --- song side helpers --------------------------------------------------------------------------

def _title_bases(song: Song) -> list[str]:
    """Song titles without short-version tags and brackets, plus the same without a 'feat.' tail."""
    out = []
    for t in (song.title_unicode, song.title):
        base = strip_brackets(strip_version_tags(t))
        for b in (base, strip_feat(base)):
            if b and b not in out:
                out.append(b)
    return out


def song_title_keys(song: Song) -> list[str]:
    keys = []
    for b in _title_bases(song):
        k = compact(b)
        if k and k not in keys:
            keys.append(k)
    return keys


_SYMBOL_RE = re.compile(r"[^\w\s]|_")


def _symbol_gap_match(song: Song, hay: str) -> bool:
    """Titles whose symbols uploaders replace or drop: 'Gregorius | Symphony' ~ 'Gregorius I Symphony',
    'TRI△NGLE' ~ 'TRIANGLE'. Each symbol may stand for at most one character in the video title."""
    for b in _title_bases(song):
        pieces = [compact(p) for p in _SYMBOL_RE.split(normalize_text(b))]
        pieces = [p for p in pieces if p]
        if len(pieces) < 2 or sum(map(len, pieces)) < 4:
            continue
        if re.search(".?".join(map(re.escape, pieces)), hay):
            return True
    return False


def artist_keys(artist: str) -> tuple[str, list[str]]:
    """(whole artist, individual names). '(CV: X)' contributes X as a name as well."""
    cv_names = _CV_RE.findall(artist or "")
    base = strip_brackets(artist)
    whole = compact(base)
    parts = [compact(p) for p in _ARTIST_SPLIT_RE.split(normalize_text(base))]
    parts += [compact(p) for name in cv_names for p in _ARTIST_SPLIT_RE.split(normalize_text(name))]
    seen, names = set(), []
    for p in parts:
        if len(p) >= 2 and p not in seen:
            seen.add(p)
            names.append(p)
    return whole, names


# --- components (each 0..1) ---------------------------------------------------------------------

def duration_score(song_sec: float | None, video_sec: float | None, full_size_mode: bool) -> float:
    if video_sec is None:
        return 0.3  # live / unknown
    if not song_sec:
        return 0.5  # nothing to compare against
    if full_size_mode:
        lo, hi = song_sec + 20, min(max(song_sec * 4, song_sec + 60), 600)
        if lo <= video_sec <= hi:
            return 1.0
        if abs(video_sec - song_sec) <= 15:
            return 0.4  # the short version itself
        if song_sec + 15 < video_sec < lo:
            return 0.7
        if video_sec > hi:
            return max(0.0, 1.0 - (video_sec - hi) / 300)
        return 0.0  # shorter than the osu! audio
    d = abs(video_sec - song_sec)
    if d <= 3:
        return 1.0
    if d <= 10:
        return 1.0 - (d - 3) / 7 * 0.4
    if d <= 30:
        return 0.6 - (d - 10) / 20 * 0.6
    return 0.0


def _containment(needle: str, hay: str) -> float:
    """1.0 if needle is in hay, otherwise the share of needle covered by the longest common block."""
    if not needle or not hay:
        return 0.0
    if needle in hay:
        return 1.0
    m = SequenceMatcher(None, needle, hay, autojunk=False).find_longest_match(0, len(needle), 0, len(hay))
    ratio = m.size / len(needle)
    return ratio if ratio >= PARTIAL_MIN else 0.0


def title_score(song: Song, video_title: str) -> float:
    hay = compact(video_title)
    best = max((_containment(k, hay) for k in song_title_keys(song)), default=0.0)
    if best < 1.0 and _symbol_gap_match(song, hay):
        return 1.0
    if best < 1.0 and song.romanised_title:
        # 'Arumagedon' ~ 'あるまげどん', 'Shoushitsu' ~ 'Shōshitsu' ~ 'Shoshitsu'
        rhay = romaji_key(video_title)
        for b in _title_bases(song):
            k = romaji_key(b)
            if len(k) >= ROMAJI_MIN_LEN:
                best = max(best, _containment(k, rhay))
    if best < SUBTITLE_CREDIT:
        for t in (song.title_unicode, song.title):
            core = compact(strip_brackets(strip_subtitle(strip_version_tags(t))))
            if core and core in hay:
                return SUBTITLE_CREDIT
    return best


def _names_in(artist: str, hay: str, romaji: bool = False) -> float:
    whole, names = artist_keys(artist)
    if romaji:  # 'AAAA' must not shrink to 'a' and match everything
        whole = romaji_key(whole) if len(romaji_key(whole)) >= ROMAJI_MIN_LEN else ""
        names = [k for k in map(romaji_key, names) if len(k) >= ROMAJI_MIN_LEN]
    if whole and whole in hay:
        return 1.0
    return sum(1 for n in names if n in hay) / len(names) if names else 0.0


def artist_score(song: Song, video_title: str, channel: str) -> float:
    hay = compact(video_title) + "|" + compact(channel)
    best = max(_names_in(a, hay) for a in (song.artist_unicode, song.artist))
    if best < 1.0 and song.romanised_artist:
        # 'rerulili' ~ 'れるりり'
        rhay = romaji_key(video_title) + "|" + romaji_key(channel)
        best = max(best, _names_in(song.artist, rhay, romaji=True))
    return best


def official_score(song: Song, channel: str, verified: bool) -> tuple[float, str | None]:
    ch = compact(channel)
    is_topic = normalize_text(channel).endswith("- topic")
    names = set()
    for artist in (song.artist_unicode, song.artist):
        whole, parts = artist_keys(artist)
        names.update([whole, *parts])
    names.discard("")
    artist_channel = any(n in ch or (len(ch) >= 3 and ch in n) for n in names) if ch else False
    if is_topic and artist_channel:
        return 1.0, "topic"
    if verified and artist_channel:
        return 1.0, "official_artist"
    if artist_channel:
        return 0.8, "artist_channel"
    if is_topic:
        return 0.7, "topic_other"
    if verified:
        return 0.6, "verified"
    if "official" in normalize_text(channel):
        return 0.4, "official_name"
    return 0.0, None


def _word_hits(words: list[str], text: str, exempt: str) -> list[str]:
    """Words present in text but not in exempt (both normalize_text'ed)."""
    hits = []
    for w in words:
        if _ASCII_WORD.match(w):
            rx = re.compile(rf"(?<![a-z0-9]){re.escape(w)}s?(?![a-z0-9])")  # plural: "instrumentals"
            if rx.search(text) and not rx.search(exempt):
                hits.append(w)
        elif w in text and w not in exempt:
            hits.append(w)
    return hits


def _song_text(song: Song, with_source: bool = False) -> str:
    titles = f"{strip_version_tags(song.title)} {strip_version_tags(song.title_unicode)}"
    src = f" {song.source}" if with_source else ""
    return normalize_text(f"{titles} {song.artist} {song.artist_unicode}{src}")


def negative_words(song: Song, video_title: str) -> list[str]:
    """Non-original recordings: covers, live, nightcore, remixes ... (unless the osu! title says so)."""
    return _word_hits(NEGATIVE_WORDS, normalize_text(video_title), _song_text(song))


def chart_signals(song: Song, video_title: str, channel: str = "", artist_upload: bool = False
                  ) -> tuple[list[str], float]:
    """Signs of a rhythm-game play / chart-preview video, and the penalty they earn.

    Anything the osu! title, artist or source itself contains is ignored (a song made for SDVX may
    well be uploaded as "[SDVX] ..."). Game names alone are a weaker signal and also give way to the
    osu! tags or to an upload by the artist themselves.
    """
    vt = normalize_text(video_title)
    ch = normalize_text(channel)
    st = _song_text(song, with_source=True)
    tags = normalize_text(song.tags)

    named_games = _word_hits(GAME_WORDS, vt, st)
    strong = _word_hits(CHART_WORDS, vt, st)
    strong += [name for name, rx in CHART_PATTERNS.items()
               if _pattern_hit(rx, vt, st) and not (artist_upload and name in _ARTIST_SAFE_PATTERNS)]
    if named_games and _word_hits(PLAY_WORDS, vt, st):
        strong.append("played")
    if not artist_upload:
        strong += [f"channel:{w}" for w in _word_hits(CHANNEL_WORDS, ch, st)]
    if strong:
        return strong, CHART_PENALTY

    if artist_upload or _word_hits(AUDIO_WORDS, vt, ""):
        return [], 0.0
    games = [w for w in named_games if w in _TAG_EXEMPT_EXCLUDED or _word_hits([w], vt, tags)]
    games += [f"channel:{w}" for w in _word_hits(CHANNEL_GAME_WORDS, ch, st)]
    if not games and _osu_metadata_bracket(song, vt):
        games.append("osu_format")
    return games, (GAME_PENALTY if games else 0.0)


def _pattern_hit(rx: re.Pattern, vt: str, st: str) -> bool:
    """rx matches vt somewhere other than a bracket that just repeats the song's own words
    ("Tanza3D - what's done is done [what's done is done]")."""
    if rx.search(st):
        return False
    for m in rx.finditer(vt):
        inner = m.group(0).strip(" []【】(){}")
        if len(inner) < 6 or not re.search(rf"(?<![a-z0-9]){re.escape(inner)}(?![a-z0-9])", st):
            return True
    return False


def _osu_metadata_bracket(song: Song, vt: str) -> bool:
    """vt has "<artist> - <title> [X]" with X looking like a difficulty name, not "[Lyrics]" etc."""
    def loose(s: str) -> str:  # spacing / punctuation may differ: "Peko Miko" vs "Pekomiko"
        return r"[\W_]*".join(re.escape(ch) for ch in compact(s))

    titles = {compact(t) for t in (song.title, song.title_unicode, strip_version_tags(song.title),
                                   strip_version_tags(song.title_unicode)) if t}
    artists = {compact(a) for a in (song.artist, song.artist_unicode) if a}
    song_text = compact(_song_text(song, with_source=True))
    if _word_hits(MUSIC_UPLOAD_WORDS, vt, _song_text(song)):
        return False
    for a in filter(None, artists):
        for t in filter(None, titles):
            # only trailing "!!" / "~" between the title and the bracket, not "」" or "）"
            m = re.search(rf"{loose(a)}[\W_]*{loose(t)}[^\w\[【\]】()（）「」『』/／|｜]*[\[【]([^\]】]{{1,60}})[\]】]", vt)
            if not m:
                continue
            x = m.group(1).strip()
            if not compact(x) or compact(x) in song_text:
                continue
            if _word_hits(BRACKET_MUSIC_WORDS + GAME_WORDS + AUDIO_WORDS + NEGATIVE_WORDS, x, ""):
                continue
            return True
    return False


# --- total --------------------------------------------------------------------------------------

def score(song: Song, c: Candidate) -> Score:
    full_mode = song.short_version
    official, official_kind = official_score(song, c.channel, c.channel_verified)
    parts = {
        "duration": duration_score(song.duration_sec, c.duration_sec, full_mode),
        "title": title_score(song, c.title),
        "artist": artist_score(song, c.title, c.channel),
        "official": official,
    }
    total = sum(WEIGHTS[k] * v for k, v in parts.items())
    notes = []
    if official_kind:
        notes.append(official_kind)
    if full_mode:
        notes.append("full_size_mode")
    neg = negative_words(song, c.title)
    if neg:
        total -= NEGATIVE_PENALTY
        parts["penalty_words"] = -NEGATIVE_PENALTY
        notes.append("words:" + ",".join(neg))
    chart, chart_penalty = chart_signals(song, c.title, c.channel, official_kind in ARTIST_UPLOAD_KINDS)
    if chart:
        total -= chart_penalty
        parts["penalty_chart"] = -chart_penalty
        notes.append("chart:" + ",".join(chart))
    if full_mode and version_tags(c.title) & SHORT_VERSION_TAGS:
        total -= SHORT_VIDEO_PENALTY
        parts["penalty_short"] = -SHORT_VIDEO_PENALTY
        notes.append("short_video")
    if c.live:
        total -= 0.2
        parts["penalty_live"] = -0.2
    return Score(total=round(max(0.0, min(1.0, total)), 4), parts={k: round(v, 4) for k, v in parts.items()}, notes=notes)


@dataclass
class Decision:
    status: str               # confirmed / needs_review
    video_id: str | None
    score: float | None
    reason: str | None        # why it needs review


EQUIVALENT_DURATION_SEC = 5  # runner-up this close in length is likely the same recording


def decide(scores: list[tuple[str, Score, float | None]], is_pack: bool = False) -> Decision:
    """scores: (video_id, Score, video_duration_sec) sorted best first.

    Auto-accept needs: a high total, the title actually present, and an artist signal (artist name
    in title/channel, or the artist's own / Topic channel). Length alone must never carry a match.
    A close runner-up only blocks auto-accept when it looks like a *different* recording
    (length differs by more than EQUIVALENT_DURATION_SEC); two uploads of the same track are
    interchangeable.
    """
    if not scores:
        return Decision("needs_review", None, None, "no_results")
    top_id, top_score, top_dur = scores[0]
    top, parts = top_score.total, top_score.parts
    if is_pack:
        return Decision("needs_review", top_id, top, "pack")
    if top < AUTO_ACCEPT_SCORE:
        return Decision("needs_review", top_id, top, "low_score")
    if parts.get("title", 0) < TITLE_REQUIRED:
        return Decision("needs_review", top_id, top, "title_mismatch")
    if parts.get("artist", 0) < ARTIST_REQUIRED and parts.get("official", 0) < OFFICIAL_ARTIST:
        return Decision("needs_review", top_id, top, "artist_mismatch")
    if top < SURE_SCORE:
        for _, other_score, other_dur in scores[1:]:
            other = other_score.total
            if top - other >= AUTO_ACCEPT_MARGIN:
                break  # sorted: the rest are even lower
            same_recording = (
                top_dur is not None and other_dur is not None
                and abs(top_dur - other_dur) <= EQUIVALENT_DURATION_SEC
            )
            if not same_recording:
                return Decision("needs_review", top_id, top, "close_second")
    return Decision("confirmed", top_id, top, None)
