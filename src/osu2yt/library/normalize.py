"""Metadata normalisation shared by the scanner and the matcher."""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePath

# "short version" markers in osu! titles, e.g. "Onegai Muscle (TV Size)".
_VERSION_PATTERNS: dict[str, str] = {
    "tv_size": r"tv\s*(?:size|ver(?:sion|\.)?|edit|mix)|tvサイズ"
               r"|anime\s*(?:size(?:\s*ver(?:sion|\.)?)?|ver(?:sion|\.)?|edit)|\b(?:op|ed)\s*ver(?:sion|\.)?",
    "cut": r"cut\s*ver(?:sion|\.)?|cut\s*edit|カット\s*ver\.?",
    "short": r"short\s*ver(?:sion|\.)?|short\s*edit|ショート\s*ver\.?",
    "game": r"game\s*(?:ver(?:sion|\.)?|size)|ゲーム\s*ver\.?|ゲームサイズ",
    "osu_edit": r"osu!?\s*(?:edit|ver(?:sion|\.)?|cut|size)",
}
_VERSION_RES = {k: re.compile(v, re.IGNORECASE) for k, v in _VERSION_PATTERNS.items()}
SHORT_VERSION_TAGS = frozenset(_VERSION_PATTERNS)

_BRACKET_GROUP_RE = re.compile(r"[\(\[（［【〔〈《<][^\(\)\[\]（）［］【】〔〕〈〉《》<>]*[\)\]）］】〕〉》>]")
# Trailing subtitle delimited by dashes or wave dashes: "Title -Called-", "Title ～Sub～".
_SUBTITLE_RE = re.compile(r"\s[-~～〜‐-―]([^-~～〜‐-―]+)[-~～〜‐-―]\s*$")
_FEAT_RE = re.compile(r"\b(?:feat|ft|featuring)\b\.?", re.IGNORECASE)
_SPACE_RE = re.compile(r"\s+")


def clean(s: str | None) -> str:
    """Trim and collapse whitespace; None -> ''."""
    if not s:
        return ""
    return _SPACE_RE.sub(" ", s).strip()


def normalize_text(s: str | None) -> str:
    """NFKC + casefold + unified 'feat.' + collapsed whitespace."""
    s = unicodedata.normalize("NFKC", clean(s)).casefold()
    s = _FEAT_RE.sub("feat.", s)
    return _SPACE_RE.sub(" ", s).strip()


_LATIN_EXTRA = {"ø": "o", "æ": "ae", "œ": "oe", "ß": "ss", "đ": "d", "ł": "l", "ı": "i", "þ": "th"}


def fold_accents(s: str) -> str:
    """'Cö shu Nie' -> 'Co shu Nie', 'café' -> 'cafe'. Only Latin letters lose their marks;
    kana keep their dakuten (ガ stays ガ)."""
    out = []
    for ch in s:
        if ch.isascii():
            out.append(ch)
            continue
        d = unicodedata.normalize("NFD", ch)
        if len(d) > 1 and d[0].isascii() and d[0].isalpha() and all(unicodedata.combining(c) for c in d[1:]):
            out.append(d[0])
        else:
            out.append(_LATIN_EXTRA.get(ch, ch))
    return "".join(out)


def compact(s: str | None) -> str:
    """normalize_text (accents folded) with every non letter/digit removed. Used for containment checks."""
    return "".join(ch for ch in fold_accents(normalize_text(s)) if ch.isalnum())


def strip_brackets(s: str | None) -> str:
    """Remove bracketed groups such as '(TV Size)' or '[Extended Mix]' (innermost first)."""
    out = clean(s)
    while True:
        new = _BRACKET_GROUP_RE.sub(" ", out)
        if new == out:
            break
        out = new
    return clean(out)


def strip_subtitle(s: str | None) -> str:
    """'エリフィと白昏の夜 -Called-' -> 'エリフィと白昏の夜'. Returns '' when there is no such subtitle."""
    t = clean(s)
    m = _SUBTITLE_RE.search(t)
    if not m:
        return ""
    return clean(t[: m.start()])


def version_tags(title: str | None) -> set[str]:
    """Short-version markers present in a title ({'tv_size', 'cut', ...})."""
    t = unicodedata.normalize("NFKC", title or "")
    return {tag for tag, rx in _VERSION_RES.items() if rx.search(t)}


def strip_version_tags(title: str | None) -> str:
    """Title with short-version markers (and the brackets around them) removed, for search queries."""
    t = unicodedata.normalize("NFKC", clean(title))
    for rx in _VERSION_RES.values():
        # bracketed form first: "(TV Size)" / "[Cut Ver.]"
        t = re.sub(rf"[\(\[（［【]\s*(?:{rx.pattern})\s*[\)\]）］】]", " ", t, flags=re.IGNORECASE)
        t = re.sub(rf"[-~〜]\s*(?:{rx.pattern})\s*[-~〜]?", " ", t, flags=re.IGNORECASE)
        t = rx.sub(" ", t)
    return clean(t)


def unicode_or(primary: str | None, fallback: str | None) -> str:
    """Prefer the Unicode field, falling back to the romanised one when empty."""
    return clean(primary) or clean(fallback)


_FEAT_TAIL_RE = re.compile(r"\s*[\(\[（［]?\s*\b(?:feat|ft|featuring)\b\.?\s.*$", re.IGNORECASE)


def strip_feat(s: str | None) -> str:
    """'Falling Down feat. Renko × TRI△NGLE' -> 'Falling Down'. '' when there is no feat. part."""
    t = clean(s)
    m = _FEAT_TAIL_RE.search(t)
    if not m or m.start() == 0:
        return ""
    return clean(t[: m.start()])


# --- kana <-> romaji -----------------------------------------------------------------------------
# Lets "アルマゲドン" in a video title match an osu! title that only exists as "Arumagedon", and
# turns romaji-only titles into a katakana search query. No dictionary: kanji stay as they are.

_KANA_TABLE = (
    "あa いi うu えe おo かka きki くku けke こko さsa しshi すsu せse そso たta ちchi つtsu てte とto "
    "なna にni ぬnu ねne のno はha ひhi ふfu へhe ほho まma みmi むmu めme もmo やya ゆyu よyo "
    "らra りri るru れre ろro わwa をwo んn がga ぎgi ぐgu げge ごgo ざza じji ずzu ぜze ぞzo "
    "だda ぢji づzu でde どdo ばba びbi ぶbu べbe ぼbo ぱpa ぴpi ぷpu ぺpe ぽpo ゔvu ゐi ゑe "
    "ぁa ぃi ぅu ぇe ぉo ゃya ゅyu ょyo ゎwa"
)
_KANA = {tok[0]: tok[1:] for tok in _KANA_TABLE.split()}
_SMALL_Y = {"ゃ": "a", "ゅ": "u", "ょ": "o"}
_SMALL_V = {"ぁ": "a", "ぃ": "i", "ぅ": "u", "ぇ": "e", "ぉ": "o"}


def _to_hiragana(s: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in s)


def kana_to_romaji(s: str | None) -> str:
    """Hepburn-ish romaji for kana; everything else is kept. 'アルマゲドン' -> 'arumagedon'."""
    t = _to_hiragana(s or "")
    out: list[str] = []
    i = 0
    while i < len(t):
        c = t[i]
        nxt = t[i + 1] if i + 1 < len(t) else ""
        if c in ("っ", "ッ"):
            r = _KANA.get(_to_hiragana(nxt), "")
            if r and r[0] not in "aiueon":
                out.append("t" if r.startswith("ch") else r[0])
            i += 1
            continue
        if c == "ー":
            last = next((ch for ch in reversed("".join(out)) if ch in "aiueo"), "")
            out.append(last)
            i += 1
            continue
        r = _KANA.get(c)
        if r is None:
            out.append(c)
            i += 1
            continue
        if nxt in _SMALL_Y and r.endswith("i") and len(r) > 1:
            base = r[:-1]
            out.append(base + _SMALL_Y[nxt] if base in ("sh", "ch", "j") else base + "y" + _SMALL_Y[nxt])
            i += 2
            continue
        if nxt in _SMALL_V and c not in _SMALL_V and (len(r) > 1 or c == "う"):
            out.append((r[:-1] or "w") + _SMALL_V[nxt])  # ファ fa, ティ ti, シェ she, ウィ wi
            i += 2
            continue
        out.append(r)
        i += 1
    return "".join(out)


def _build_romaji_to_kana() -> dict[str, str]:
    table: dict[str, str] = {}
    for kana, roma in _KANA.items():
        if kana in _SMALL_Y or kana in _SMALL_V or kana in ("ゎ", "ゐ", "ゑ", "ん"):
            continue
        table.setdefault(roma, kana)
    for kana in "きしちにひみりぎじびぴ":
        base = _KANA[kana][:-1]
        for small, v in _SMALL_Y.items():
            table.setdefault(base + v if base in ("sh", "ch", "j") else base + "y" + v, kana + small)
    extra = {"fa": "ふぁ", "fi": "ふぃ", "fe": "ふぇ", "fo": "ふぉ", "ti": "てぃ", "di": "でぃ", "va": "ゔぁ",
             "vi": "ゔぃ", "ve": "ゔぇ", "vo": "ゔぉ", "she": "しぇ", "che": "ちぇ", "je": "じぇ", "wi": "うぃ",
             "we": "うぇ", "si": "し", "tu": "つ", "hu": "ふ", "zi": "じ", "sya": "しゃ", "syu": "しゅ",
             "syo": "しょ", "tya": "ちゃ", "tyu": "ちゅ", "tyo": "ちょ", "jya": "じゃ", "jyu": "じゅ", "jyo": "じょ"}
    for roma, kana in extra.items():
        table.setdefault(roma, kana)
    return table


_ROMA2KANA = _build_romaji_to_kana()


def romaji_word_to_kana(word: str) -> str | None:
    """'arumagedon' -> 'あるまげどん'. None when the word cannot be read as romaji (e.g. 'girl').
    'l' reads as 'r' ('rerulili')."""
    w = word.lower().replace("l", "r")
    out: list[str] = []
    i = 0
    while i < len(w):
        c = w[i]
        nxt = w[i + 1] if i + 1 < len(w) else ""
        if c == "n" and (not nxt or nxt not in "aiueoy"):
            out.append("ん")
            i += 2 if nxt in ("n", "'") and (i + 2 >= len(w) or w[i + 2] not in "aiueoy") else 1
            continue
        if c not in "aiueon" and (nxt == c or (c == "t" and w[i + 1:i + 3] == "ch")):
            out.append("っ")
            i += 1
            continue
        for size in (3, 2, 1):
            kana = _ROMA2KANA.get(w[i:i + size])
            if kana:
                out.append(kana)
                i += size
                break
        else:
            return None
    return "".join(out)


_ASCII_WORD_RE = re.compile(r"[A-Za-z]+")


def looks_romaji(s: str | None) -> bool:
    """True when every word of 2+ letters reads as romaji ('Hoshizora no kanran -sha-')."""
    words = [w for w in _ASCII_WORD_RE.findall(fold_accents(s or "")) if len(w) >= 2]
    return bool(words) and all(romaji_word_to_kana(w) is not None for w in words)


def romaji_to_katakana(s: str | None) -> str:
    """Romaji words -> katakana, other words untouched. 'Arumagedon' -> 'アルマゲドン'."""
    def conv(m: re.Match) -> str:
        kana = romaji_word_to_kana(m.group(0)) if len(m.group(0)) >= 2 else None
        return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in kana) if kana else m.group(0)
    return _ASCII_WORD_RE.sub(conv, fold_accents(clean(s)))


_JAPANESE_RE = re.compile(r"[぀-ヿ㐀-䶿一-鿿ｦ-ﾟ]")


def has_japanese(s: str | None) -> bool:
    return bool(_JAPANESE_RE.search(s or ""))


# Spelling variants of the same romaji: Shōshitsu / Shoushitsu / Shoshitsu, rerulili / rerurili ...
_ROMAJI_RULES = [
    (re.compile(r"l"), "r"),
    (re.compile(r"m(?=[bp])"), "n"),
    (re.compile(r"wo"), "o"),
    (re.compile(r"ou|oh(?=[^aiueo]|$)"), "o"),
    (re.compile(r"([aiueo])\1+"), r"\1"),
]


def romaji_key(s: str | None) -> str:
    """compact() with kana read as romaji and romaji spelling variants unified. Fallback matching only."""
    out = compact(kana_to_romaji(normalize_text(s)))
    for rx, rep in _ROMAJI_RULES:
        out = rx.sub(rep, out)
    return out


# --- pack / rate-edit detection ---------------------------------------------------------------

_RATE_NOISE_RE = re.compile(
    r"\d+(?:\.\d+)?x?|\bx\b|rate|speed|sped\s*up|spedup|nightcore|\bdt\b|\bht\b|[\s_\-.()\[\]]",
    re.IGNORECASE,
)


def audio_stem(audio_file: str | None) -> str:
    """Audio file name reduced so that rate edits collapse: 'audio 1.100x.mp3' -> 'audio'."""
    if not audio_file:
        return ""
    stem = PurePath(audio_file).stem
    return _RATE_NOISE_RE.sub("", unicodedata.normalize("NFKC", stem).casefold())


def _same_song_title(a: str, b: str) -> bool:
    ca, cb = compact(strip_brackets(a)), compact(strip_brackets(b))
    if not ca or not cb:
        return True
    return ca in cb or cb in ca


def is_pack(variants: list[dict]) -> bool:
    """True when a beatmap set bundles several different songs.

    - two variants whose titles are unrelated  -> pack (e.g. two different songs)
    - 3+ distinct audio stems after rate-edit noise removal -> pack (e.g. OST packs that keep
      one set title but ship one audio file per song)
    Rate edits ('audio110.mp3', 'audio 1.200x.mp3') and minor title variants are not packs.
    """
    if len(variants) < 2:
        return False
    titles = [v.get("title") or "" for v in variants]
    for i in range(len(titles)):
        for j in range(i + 1, len(titles)):
            if not _same_song_title(titles[i], titles[j]):
                return True
    stems = {audio_stem(v.get("audioFile")) for v in variants}
    return len(stems) >= 3


def primary_variant(variants: list[dict]) -> dict | None:
    """The variant representing the set: most difficulties, then longest (rate edits are shorter)."""
    if not variants:
        return None
    return max(variants, key=lambda v: (v.get("difficultyCount") or 0, v.get("maxLengthMs") or 0))
