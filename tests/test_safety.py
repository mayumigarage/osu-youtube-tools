"""Guards for the spec's safety rules: never write to the original client.realm, bind to 127.0.0.1 only,
keep credentials out of git and logs."""

import hashlib
import json
import os
import shutil
import socket
from pathlib import Path

import pytest

from osu2yt import config, db
from osu2yt.library import helper, realm_copy, scanner
from osu2yt.library.realm_copy import RealmCopyError, copy_realm

ROOT = Path(__file__).resolve().parents[1]


def digest(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def lazer(tmp_path):
    d = tmp_path / "osu"
    d.mkdir()
    (d / "client.realm").write_bytes(os.urandom(4096))
    return d


# --- client.realm is only ever read ---------------------------------------------------------------

def test_copy_leaves_original_untouched(lazer, tmp_path):
    src = lazer / "client.realm"
    before = (digest(src), src.stat().st_mtime_ns)
    dest, st = copy_realm(lazer, tmp_path / "work")
    assert dest.parent == tmp_path / "work" and digest(dest) == before[0]
    assert (digest(src), src.stat().st_mtime_ns) == before
    assert sorted(p.name for p in lazer.iterdir()) == ["client.realm"]   # no lock/management files next to it


def test_copy_retries_while_lazer_is_writing(lazer, tmp_path, monkeypatch):
    real_copy = shutil.copyfile
    calls = []

    def copy_while_writing(src, dst):
        calls.append(1)
        out = real_copy(src, dst)
        if len(calls) == 1:   # lazer writes during the first copy
            Path(src).write_bytes(os.urandom(5000))
        return out

    monkeypatch.setattr(realm_copy.shutil, "copyfile", copy_while_writing)
    dest, st = copy_realm(lazer, tmp_path / "work", delay=0)
    assert len(calls) == 2 and st.size == 5000 and dest.stat().st_size == 5000


def test_copy_gives_up_when_never_stable(lazer, tmp_path, monkeypatch):
    real_copy = shutil.copyfile

    def always_changing(src, dst):
        real_copy(src, dst)
        Path(src).write_bytes(os.urandom(4096 + len(list(Path(dst).parent.iterdir()))))
        os.utime(src, (1, os.stat(src).st_mtime + 5))

    monkeypatch.setattr(realm_copy.shutil, "copyfile", always_changing)
    with pytest.raises(RealmCopyError, match="安定してコピーできません"):
        copy_realm(lazer, tmp_path / "work", retries=2, delay=0)
    assert not (tmp_path / "work" / "client.realm.copying").exists()


def test_copy_access_denied_tells_user_to_close_lazer(lazer, tmp_path, monkeypatch):
    def denied(src, dst):
        raise PermissionError("locked")

    monkeypatch.setattr(realm_copy.shutil, "copyfile", denied)
    with pytest.raises(RealmCopyError, match="osu!lazer を終了"):
        copy_realm(lazer, tmp_path / "work", retries=2, delay=0)


def test_copy_replaces_stale_realm_side_files(lazer, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "client.realm.lock").write_text("x")
    (work / "client.realm.management").mkdir()
    copy_realm(lazer, work)
    assert sorted(p.name for p in work.iterdir()) == ["client.realm"]


def test_missing_realm(tmp_path):
    with pytest.raises(RealmCopyError, match="見つかりません"):
        copy_realm(tmp_path, tmp_path / "work")


def test_scan_hands_only_the_copy_to_the_helper(lazer, tmp_path, monkeypatch):
    seen = []

    def fake_export(realm, out):
        seen.append(Path(realm))
        return {"format": 1, "warnings": [], "sets": []}

    monkeypatch.setattr(helper, "export", fake_export)
    conn = db.connect(":memory:")
    scanner.scan(conn, lazer, tmp_path / "work")
    assert seen == [tmp_path / "work" / "client.realm"]


def test_helper_opens_realm_read_only():
    src = (ROOT / "helper" / "OsuRealmExport" / "Program.cs").read_text(encoding="utf-8")
    assert "IsReadOnly = true" in src and "IsDynamic = true" in src


# --- network / secrets -----------------------------------------------------------------------------

def test_serve_binds_loopback_only(monkeypatch, tmp_path):
    import uvicorn

    from osu2yt import __main__ as cli
    from osu2yt.scheduler import AutoResume

    monkeypatch.setattr(config, "DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(AutoResume, "start", lambda self: None)
    captured = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: captured.update(kw))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert cli.serve(port, open_browser=False) == 0
    assert captured["host"] == "127.0.0.1" and captured["port"] == port


def test_credentials_live_in_gitignored_data_dir():
    from osu2yt.youtube import auth

    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    assert {"data/", "client_secret*.json", "token*.json"} <= set(ignored)
    if not os.environ.get("OSU2YT_DATA"):
        assert config.DATA_DIR == ROOT / "data"
    assert auth.client_secret_path().parent == config.DATA_DIR
    assert auth.token_path().parent == config.DATA_DIR


@pytest.mark.parametrize("content, match", [
    ({"web": {"client_id": "x"}}, "デスクトップアプリ"),
    ("not json", "読み込めません"),
])
def test_client_secret_must_be_desktop_app(tmp_path, content, match):
    from osu2yt.youtube import auth

    p = tmp_path / "client_secret.json"
    p.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")
    with pytest.raises(auth.AuthError, match=match):
        auth.check_client_secret(p)
    with pytest.raises(auth.AuthError, match="README"):
        auth.check_client_secret(tmp_path / "missing.json")


def test_log_records_are_masked(caplog):
    import logging

    from osu2yt import logging_setup

    logging_setup.setup()
    log = logging.getLogger("osu2yt.test")
    with caplog.at_level(logging.INFO):
        log.info("refresh with %s and secret GOCSPX-abcdefghijklmnop", "1//0gAbCdEfGhIjKlMnOp")
    text = caplog.text
    assert "GOCSPX-abcdefghijklmnop" not in text and "1//0gAbCdEfGhIjKlMnOp" not in text


# --- optional: real osu!lazer data ------------------------------------------------------------------

@pytest.mark.skipif(not os.environ.get("OSU2YT_INTEGRATION"), reason="set OSU2YT_INTEGRATION=1 to run against real data")
def test_real_library_export_does_not_touch_original(tmp_path):
    lazer_dir = config.default_lazer_dir()
    src = lazer_dir / "client.realm"
    if not src.is_file() or helper.needs_build():
        pytest.skip("osu!lazer data or built helper not available")
    before = src.stat().st_mtime_ns
    dest, _ = copy_realm(lazer_dir, tmp_path)
    data = helper.export(dest, tmp_path / "export.json")
    assert data["format"] == 1 and data["sets"]
    # lazer itself may write while running, so only check we did not create files next to the original
    assert not (lazer_dir / "client.realm.lock").exists() or src.stat().st_mtime_ns >= before
