"""Filesystem locations. Everything the tool writes lives under DATA_DIR (git-ignored)."""

from __future__ import annotations

import os
from pathlib import Path

# src/osu2yt/config.py -> project root (the tool is run from a source checkout / editable install)
PROJECT_ROOT = Path(__file__).resolve().parents[2]

DATA_DIR = Path(os.environ.get("OSU2YT_DATA") or PROJECT_ROOT / "data")
DB_PATH = DATA_DIR / "osu2yt.db"
WORK_DIR = DATA_DIR / "work"

HELPER_PROJECT_DIR = PROJECT_ROOT / "helper" / "OsuRealmExport"
HELPER_PUBLISH_DIR = HELPER_PROJECT_DIR / "bin" / "publish"


def default_lazer_dir() -> Path:
    """osu!lazer's default data directory for the current OS."""
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "osu"
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "osu"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    WORK_DIR.mkdir(parents=True, exist_ok=True)
