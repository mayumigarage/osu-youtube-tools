import copy

import pytest

from osu2yt import db
from osu2yt.library import scanner
from osu2yt.library.scanner import apply_export, build_records, diff_keys, fill_durations, set_key


def make_set(online_id, title="Song", artist="Artist", *, guid=None, delete=False, protected=False,
             audio_hash="h1", title_u=None, artist_u=None, length_ms=120_000, diffs=1):
    return {
        "id": guid or f"guid-{online_id}",
        "onlineId": online_id,
        "dateAdded": "2025-01-01T00:00:00+00:00",
        "deletePending": delete,
        "protected": protected,
        "difficultyCount": diffs,
        "maxLengthMs": length_ms,
        "variants": [{
            "title": title, "titleUnicode": title_u, "artist": artist, "artistUnicode": artist_u,
            "source": "", "tags": "", "audioFile": "audio.mp3", "audioHash": audio_hash,
            "maxLengthMs": length_ms, "difficultyCount": diffs,
        }],
    }


def export(*sets):
    return {"format": 1, "warnings": [], "sets": list(sets)}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


def keys(conn, where="1=1"):
    return {r[0] for r in conn.execute(f"SELECT key FROM beatmapsets WHERE {where}")}


def test_set_key():
    assert set_key({"onlineId": 123, "id": "g"}) == "o:123"
    assert set_key({"onlineId": -1, "id": "abc"}) == "g:abc"
    assert set_key({"onlineId": 0, "id": "abc"}) == "g:abc"
    assert set_key({"onlineId": None}) is None


class TestBuildRecords:
    def test_excludes_delete_pending_and_protected(self):
        recs = build_records(export(make_set(1), make_set(2, delete=True), make_set(-1, guid="t", protected=True)))
        assert set(recs) == {"o:1"}

    def test_include_protected(self):
        recs = build_records(export(make_set(-1, guid="t", protected=True)), include_protected=True)
        assert set(recs) == {"g:t"}

    def test_unicode_falls_back_to_romanised(self):
        r = build_records(export(make_set(1, "Onegai", "Hibiki", title_u=None, artist_u="")))["o:1"]
        assert (r.title_unicode, r.artist_unicode) == ("Onegai", "Hibiki")
        r = build_records(export(make_set(1, "Onegai", "Hibiki", title_u="お願い", artist_u="ひびき")))["o:1"]
        assert (r.title_unicode, r.artist_unicode) == ("お願い", "ひびき")

    def test_map_length_in_seconds(self):
        r = build_records(export(make_set(1, length_ms=87_671)))["o:1"]
        assert r.map_length_sec == pytest.approx(87.671)
        assert build_records(export(make_set(2, length_ms=0)))["o:2"].map_length_sec is None

    def test_duplicate_online_id_keeps_more_difficulties(self):
        a = make_set(5, title="Few", diffs=1, guid="a")
        b = make_set(5, title="Many", diffs=4, guid="b")
        assert build_records(export(a, b))["o:5"].title == "Many"
        assert build_records(export(b, a))["o:5"].title == "Many"


def test_diff_keys():
    existing = {"a": True, "b": True, "c": False, "d": False}
    d = diff_keys(existing, ["a", "c", "e"])
    assert d.new == {"e"}
    assert d.kept == {"a", "c"}
    assert d.gone == {"b"}          # was in library, now missing
    assert d.returned == {"c"}      # was gone, now back
    # 'd' stays gone and is not reported again


class TestApplyExport:
    def test_first_scan_inserts_unmatched(self, conn):
        diff, _ = apply_export(conn, export(make_set(1), make_set(2)), scan_id=1)
        assert diff.new == {"o:1", "o:2"}
        assert keys(conn, "match_status='unmatched' AND in_library=1") == {"o:1", "o:2"}

    def test_rescan_preserves_match_and_tracks_gone(self, conn):
        apply_export(conn, export(make_set(1), make_set(2), make_set(3)), scan_id=1)
        conn.execute("UPDATE beatmapsets SET match_status='confirmed', chosen_video_id='vid1' WHERE key='o:1'")

        # set 1 metadata changed, set 2 removed, set 3 delete-pending, set 4 new
        second = export(make_set(1, title="Song (new title)"), make_set(3, delete=True), make_set(4))
        diff, _ = apply_export(conn, second, scan_id=2)
        assert diff.new == {"o:4"}
        assert diff.gone == {"o:2", "o:3"}

        row = conn.execute("SELECT * FROM beatmapsets WHERE key='o:1'").fetchone()
        assert (row["title"], row["match_status"], row["chosen_video_id"]) == ("Song (new title)", "confirmed", "vid1")
        assert row["last_seen_scan"] == 2 and row["first_seen_scan"] == 1
        assert keys(conn, "in_library=0") == {"o:2", "o:3"}

        # set 2 comes back
        diff, _ = apply_export(conn, copy.deepcopy(second) | {"sets": second["sets"] + [make_set(2)]}, scan_id=3)
        assert diff.returned == {"o:2"} and not diff.new
        assert keys(conn, "in_library=0") == {"o:3"}

    def test_audio_change_resets_duration(self, conn):
        apply_export(conn, export(make_set(1, audio_hash="h1"), make_set(2, audio_hash="h2")), scan_id=1)
        conn.execute("UPDATE beatmapsets SET duration_sec=100, duration_source='audio'")
        apply_export(conn, export(make_set(1, audio_hash="h1"), make_set(2, audio_hash="h2b")), scan_id=2)
        rows = {r["key"]: r for r in conn.execute("SELECT * FROM beatmapsets")}
        assert rows["o:1"]["duration_source"] == "audio"
        assert rows["o:2"]["duration_source"] is None and rows["o:2"]["audio_hash"] == "h2b"


class TestFillDurations:
    def test_prefers_audio_then_map(self, conn, monkeypatch, tmp_path):
        calls = []

        def fake_probe(lazer_dir, hashes, progress=None, **_):
            hashes = list(hashes)
            calls.append(hashes)
            return {h: (131.1 if h == "h1" else None) for h in hashes}

        monkeypatch.setattr(scanner.audio, "probe_many", fake_probe)
        apply_export(conn, export(make_set(1, audio_hash="h1"), make_set(2, audio_hash="h2", length_ms=90_000),
                                  make_set(3, audio_hash=None, length_ms=0)), scan_id=1)
        assert fill_durations(conn, tmp_path) == 2
        rows = {r["key"]: r for r in conn.execute("SELECT * FROM beatmapsets")}
        assert (rows["o:1"]["duration_sec"], rows["o:1"]["duration_source"]) == (131.1, "audio")
        assert (rows["o:2"]["duration_sec"], rows["o:2"]["duration_source"]) == (90.0, "map")
        assert rows["o:3"]["duration_source"] == "unknown"

        # failures are cached: nothing is probed again
        assert fill_durations(conn, tmp_path) == 0
        assert len(calls) == 1

    def test_without_ffprobe_falls_back_and_retries_later(self, conn, monkeypatch, tmp_path):
        monkeypatch.setattr(scanner.audio, "probe_many", lambda *a, **k: {})
        apply_export(conn, export(make_set(1, audio_hash="h1", length_ms=90_000)), scan_id=1)
        fill_durations(conn, tmp_path)
        assert conn.execute("SELECT duration_source FROM beatmapsets").fetchone()[0] == "map"

        monkeypatch.setattr(scanner.audio, "probe_many", lambda lazer, hashes, **k: {h: 95.5 for h in hashes})
        assert fill_durations(conn, tmp_path) == 1
        assert conn.execute("SELECT duration_sec, duration_source FROM beatmapsets").fetchone()[:] == (95.5, "audio")
