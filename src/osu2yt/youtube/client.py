"""YouTube Data API v3 wrapper: quota accounting, retries with exponential backoff, typed errors."""

from __future__ import annotations

import json
import logging
import random
import socket
import sqlite3
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import httplib2
from googleapiclient.errors import HttpError

from . import quota

log = logging.getLogger(__name__)

PRIVACY_VALUES = ("private", "unlisted", "public")

# reasons that are worth retrying (transient)
RETRY_REASONS = {"backendError", "internalError", "rateLimitExceeded", "userRateLimitExceeded", "SERVICE_UNAVAILABLE"}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}
RETRY_STATUS = {500, 502, 503, 504}
NETWORK_ERRORS = (OSError, socket.timeout, ssl.SSLError, httplib2.HttpLib2Error, TimeoutError, ConnectionError)


class YouTubeError(RuntimeError):
    pass


class QuotaExceeded(YouTubeError):
    """Daily quota is used up (by Google's count, or our local budget). Stop and resume tomorrow."""


class NotFound(YouTubeError):
    pass


class NoChannel(YouTubeError):
    """The logged-in identity has no YouTube channel (playlists belong to channels)."""

    def __init__(self):
        super().__init__(
            "ログイン中のアカウントに YouTube チャンネルがありません。"
            "https://www.youtube.com/create_channel でチャンネルを作成するか、"
            "ブランドアカウントのチャンネルを使う場合は `auth logout` → `auth login` でそのチャンネルを選び直してください。"
        )


def error_reason(e: HttpError) -> str | None:
    try:
        data = json.loads(e.content.decode("utf-8") if isinstance(e.content, bytes) else e.content)
        err = data.get("error", {})
        errors = err.get("errors") or []
        if errors and errors[0].get("reason"):
            return errors[0]["reason"]
        return err.get("status")
    except (ValueError, AttributeError):
        return None


@dataclass
class Playlist:
    id: str
    title: str
    privacy: str
    item_count: int | None = None


@dataclass
class PlaylistItem:
    item_id: str
    video_id: str


class YouTubeClient:
    def __init__(
        self,
        service: Any,
        conn: sqlite3.Connection,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        enforce_budget: bool = True,
    ):
        self.service = service
        self.conn = conn
        self.max_attempts = max_attempts
        self.base_delay = base_delay
        self.sleep = sleep
        self.enforce_budget = enforce_budget

    @classmethod
    def from_saved_credentials(cls, conn: sqlite3.Connection, **kw) -> "YouTubeClient":
        from googleapiclient.discovery import build

        from .auth import require_credentials

        service = build("youtube", "v3", credentials=require_credentials(), cache_discovery=False)
        return cls(service, conn, **kw)

    # --- core ------------------------------------------------------------------------------------

    def _execute(self, method: str, make_request: Callable[[], Any]) -> dict:
        units = quota.COSTS[method]
        for attempt in range(self.max_attempts):
            if self.enforce_budget and not quota.can_spend(self.conn, units):
                st = quota.status(self.conn)
                raise QuotaExceeded(
                    f"本日のクォータ予算を使い切りました ({st.used}/{st.limit}, 余裕 {st.margin})。"
                    f"{st.reset_at.astimezone():%m/%d %H:%M} 以降に再開してください。"
                )
            quota.record(self.conn, method, units)
            try:
                return make_request().execute(num_retries=0)
            except HttpError as e:
                reason = error_reason(e)
                status = e.resp.status if e.resp is not None else None
                if reason in QUOTA_REASONS:
                    raise QuotaExceeded(f"YouTube API のクォータ上限に達しました ({reason})。翌日に再開してください。") from e
                if reason in ("channelNotFound", "youtubeSignupRequired"):
                    raise NoChannel() from e
                if status == 404 or reason in ("playlistNotFound", "videoNotFound"):
                    raise NotFound(f"{method}: {reason or status}") from e
                transient = status in RETRY_STATUS or reason in RETRY_REASONS or (status == 409 and reason == "SERVICE_UNAVAILABLE")
                if not transient or attempt == self.max_attempts - 1:
                    raise YouTubeError(f"{method} failed: HTTP {status} {reason or ''}".strip()) from e
                why = f"HTTP {status} {reason}"
            except NETWORK_ERRORS as e:
                if attempt == self.max_attempts - 1:
                    raise YouTubeError(f"{method} failed: network error {type(e).__name__}") from e
                why = type(e).__name__
            delay = self.base_delay * (2 ** attempt) + random.uniform(0, self.base_delay)
            log.warning("%s: %s, retry %d/%d in %.1fs", method, why, attempt + 1, self.max_attempts - 1, delay)
            self.sleep(delay)
        raise AssertionError("unreachable")

    # --- playlists -------------------------------------------------------------------------------

    def list_my_playlists(self) -> list[Playlist]:
        out, token = [], None
        while True:
            resp = self._execute("playlists.list", lambda: self.service.playlists().list(
                part="snippet,status,contentDetails", mine=True, maxResults=50, pageToken=token))
            for p in resp.get("items", []):
                out.append(Playlist(
                    p["id"], p["snippet"]["title"], p.get("status", {}).get("privacyStatus", "?"),
                    p.get("contentDetails", {}).get("itemCount"),
                ))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def create_playlist(self, title: str, privacy: str = "private", description: str = "") -> Playlist:
        if privacy not in PRIVACY_VALUES:
            raise ValueError(f"privacy must be one of {PRIVACY_VALUES}")
        body = {"snippet": {"title": title, "description": description}, "status": {"privacyStatus": privacy}}
        resp = self._execute("playlists.insert", lambda: self.service.playlists().insert(part="snippet,status", body=body))
        return Playlist(resp["id"], resp["snippet"]["title"], resp["status"]["privacyStatus"], 0)

    def list_playlist_items(self, playlist_id: str) -> list[PlaylistItem]:
        out, token = [], None
        while True:
            resp = self._execute("playlistItems.list", lambda: self.service.playlistItems().list(
                part="contentDetails", playlistId=playlist_id, maxResults=50, pageToken=token))
            for it in resp.get("items", []):
                vid = it.get("contentDetails", {}).get("videoId")
                if vid:
                    out.append(PlaylistItem(it["id"], vid))
            token = resp.get("nextPageToken")
            if not token:
                return out

    def insert_playlist_item(self, playlist_id: str, video_id: str) -> str:
        body = {"snippet": {"playlistId": playlist_id, "resourceId": {"kind": "youtube#video", "videoId": video_id}}}
        resp = self._execute("playlistItems.insert", lambda: self.service.playlistItems().insert(part="snippet", body=body))
        return resp["id"]

    def delete_playlist_item(self, item_id: str) -> None:
        self._execute("playlistItems.delete", lambda: self.service.playlistItems().delete(id=item_id))

    # --- videos ----------------------------------------------------------------------------------

    def check_videos(self, video_ids: Iterable[str], region: str = "JP") -> dict[str, str]:
        """Availability per video: ok / deleted / private / region_blocked / not_embeddable.

        videos.list costs 1 unit per call of up to 50 IDs. Videos missing from the response are
        deleted (or never existed).
        """
        ids = list(dict.fromkeys(video_ids))
        result: dict[str, str] = {}
        for i in range(0, len(ids), 50):
            chunk = ids[i:i + 50]
            resp = self._execute("videos.list", lambda: self.service.videos().list(
                part="status,contentDetails", id=",".join(chunk), maxResults=50))
            found = {v["id"]: v for v in resp.get("items", [])}
            for vid in chunk:
                result[vid] = availability(found.get(vid), region)
        return result


def availability(video: dict | None, region: str = "JP") -> str:
    if video is None:
        return "deleted"
    status = video.get("status", {})
    if status.get("uploadStatus") in ("deleted", "rejected", "failed"):
        return "deleted"
    if status.get("privacyStatus") == "private":
        return "private"
    rr = video.get("contentDetails", {}).get("regionRestriction") or {}
    if region in rr.get("blocked", []) or ("allowed" in rr and region not in rr["allowed"]):
        return "region_blocked"
    if status.get("embeddable") is False:
        return "not_embeddable"  # still playable on YouTube; only the WebUI preview is affected
    return "ok"


PLAYABLE = {"ok", "not_embeddable"}
