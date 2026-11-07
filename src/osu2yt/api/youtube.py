"""Google login, playlists and video checks."""

from __future__ import annotations

import sqlite3
import threading
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..logging_setup import mask
from ..youtube import auth, playlists
from ..youtube.client import QuotaExceeded, YouTubeClient, YouTubeError
from .core import setting
from .deps import get_conn

router = APIRouter(prefix="/api")


class _LoginState:
    """Login runs the loopback flow in a thread: it opens the browser and waits for consent."""

    def __init__(self):
        self.lock = threading.Lock()
        self.status = "idle"     # idle / waiting / done / error
        self.error: str | None = None

    def start(self) -> None:
        with self.lock:
            if self.status == "waiting":
                return
            self.status, self.error = "waiting", None
        threading.Thread(target=self._run, name="google-login", daemon=True).start()

    def _run(self) -> None:
        try:
            auth.login_interactive(open_browser=True)
            self.status = "done"
        except Exception as e:  # noqa: BLE001 - shown in UI
            self.status, self.error = "error", mask(str(e))


login_state = _LoginState()


def _client(conn: sqlite3.Connection) -> YouTubeClient:
    try:
        return YouTubeClient.from_saved_credentials(conn)
    except auth.AuthError as e:
        raise HTTPException(401, str(e)) from e


def _api_errors(fn):
    try:
        return fn()
    except QuotaExceeded as e:
        raise HTTPException(429, str(e)) from e
    except auth.AuthError as e:
        raise HTTPException(401, str(e)) from e
    except YouTubeError as e:
        raise HTTPException(502, mask(str(e))) from e


@router.get("/auth")
def auth_status():
    return {"login": login_state.status, "error": login_state.error}


@router.post("/auth/login")
def login():
    try:
        auth.check_client_secret()
    except auth.AuthError as e:
        raise HTTPException(400, str(e)) from e
    login_state.start()
    return auth_status()


@router.post("/auth/logout")
def logout():
    existed = auth.logout()
    login_state.status = "idle"
    return {"logged_out": existed}


# --- playlists ---------------------------------------------------------------------------------------

@router.get("/playlists")
def cached_playlists(conn: sqlite3.Connection = Depends(get_conn)):
    active = playlists.active(conn)
    rows = conn.execute("SELECT * FROM playlists ORDER BY created_at DESC, title").fetchall()
    return {"active": active["id"] if active else None, "playlists": [dict(r) for r in rows]}


@router.post("/playlists/refresh")
def refresh_playlists(conn: sqlite3.Connection = Depends(get_conn)):
    _api_errors(lambda: playlists.refresh_my_playlists(conn, _client(conn)))
    return cached_playlists(conn)


class CreatePlaylist(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    privacy: Literal["private", "unlisted", "public"] = "private"
    description: str = Field("", max_length=5000)


@router.post("/playlists")
def create_playlist(body: CreatePlaylist, conn: sqlite3.Connection = Depends(get_conn)):
    _api_errors(lambda: playlists.create(conn, _client(conn), body.title, body.privacy, body.description))
    return cached_playlists(conn)


class UsePlaylist(BaseModel):
    id: str = Field(min_length=2, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


@router.put("/playlists/active")
def use_playlist(body: UsePlaylist, conn: sqlite3.Connection = Depends(get_conn)):
    playlists.set_active(conn, body.id)
    return cached_playlists(conn)


@router.post("/playlists/pull")
def pull_playlist(conn: sqlite3.Connection = Depends(get_conn)):
    active = playlists.active(conn)
    if not active:
        raise HTTPException(400, "プレイリストが選択されていません")
    r = _api_errors(lambda: playlists.pull_items(conn, _client(conn), active["id"]))
    return {"total": r.total, "new_remote": r.new_remote, "went_missing": r.went_missing, "duplicates": r.duplicates}


@router.post("/videos/check")
def check_videos(force: bool = False, conn: sqlite3.Connection = Depends(get_conn)):
    region = setting(conn, "region")
    r = _api_errors(lambda: playlists.check_chosen_videos(conn, _client(conn), region=region, force=force))
    return {"checked": r.checked, "unavailable": r.unavailable, "sets_flagged": r.sets_flagged}
