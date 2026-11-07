"""YouTube search through yt-dlp (no Data API quota used)."""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import asdict, dataclass

import yt_dlp
from yt_dlp.utils import DownloadError

log = logging.getLogger(__name__)

_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
_URL_ID_RES = [
    re.compile(r"(?:youtube\.com|youtube-nocookie\.com)/(?:watch\?(?:.*&)?v=|embed/|shorts/|live/|v/)([A-Za-z0-9_-]{11})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),
]


class SearchError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    video_id: str
    title: str
    channel: str
    channel_id: str | None
    channel_verified: bool
    duration_sec: float | None
    live: bool = False

    @property
    def thumbnail_url(self) -> str:
        return f"https://i.ytimg.com/vi/{self.video_id}/mqdefault.jpg"

    def to_dict(self) -> dict:
        return asdict(self) | {"thumbnail_url": self.thumbnail_url}


def parse_video_id(text: str) -> str | None:
    """Video ID from a raw ID or any common YouTube URL form."""
    text = (text or "").strip()
    if _VIDEO_ID_RE.match(text):
        return text
    for rx in _URL_ID_RES:
        m = rx.search(text)
        if m:
            return m.group(1)
    return None


class _QuietLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): log.debug("yt-dlp: %s", msg)
    def error(self, msg): log.debug("yt-dlp: %s", msg)


_YDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "skip_download": True,
    "logger": _QuietLogger(),
    "socket_timeout": 20,
}


def _entry_to_info(e: dict) -> VideoInfo | None:
    vid = e.get("id")
    if not vid or not _VIDEO_ID_RE.match(vid):
        return None  # channels / playlists in results
    return VideoInfo(
        video_id=vid,
        title=e.get("title") or "",
        channel=e.get("channel") or e.get("uploader") or "",
        channel_id=e.get("channel_id"),
        channel_verified=bool(e.get("channel_is_verified")),
        duration_sec=float(e["duration"]) if e.get("duration") else None,
        live=e.get("live_status") in ("is_live", "is_upcoming"),
    )


def _with_retry(fn, what: str, attempts: int = 4, base_delay: float = 1.5):
    for attempt in range(attempts):
        try:
            return fn()
        except DownloadError as e:
            msg = str(e)
            if attempt == attempts - 1:
                raise SearchError(f"{what}: {msg}") from e
            delay = base_delay * (2 ** attempt) * (4 if "429" in msg else 1) + random.uniform(0, 0.5)
            log.warning("%s failed (%s), retrying in %.1fs", what, msg.splitlines()[0][:120], delay)
            time.sleep(delay)


def search(query: str, n: int = 5) -> list[VideoInfo]:
    def run():
        with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
            return ydl.extract_info(f"ytsearch{n}:{query}", download=False)

    result = _with_retry(run, f"search {query!r}") or {}
    infos = [_entry_to_info(e) for e in (result.get("entries") or []) if e]
    return [i for i in infos if i]


def fetch_video(video_id: str) -> VideoInfo | None:
    """Metadata for one video (used for manually entered IDs). None if unavailable."""
    opts = _YDL_OPTS | {"extract_flat": False}

    def run():
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}", download=False, process=False)

    try:
        info = _with_retry(run, f"video {video_id}", attempts=2)
    except SearchError as e:
        log.info("video %s unavailable: %s", video_id, e)
        return None
    return _entry_to_info(info or {})
