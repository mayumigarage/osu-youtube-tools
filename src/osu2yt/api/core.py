"""Status, settings, jobs, scan and matching endpoints."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

import yt_dlp
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .. import config, db
from ..jobs import Job, JobRunner
from ..library import audio, helper
from ..library.scanner import scan
from ..matching import matcher
from ..youtube import auth, playlists, quota
from .deps import get_conn, get_runner, start_job

router = APIRouter(prefix="/api")

SETTING_DEFAULTS = {
    "lazer_path": lambda: str(config.default_lazer_dir()),
    "region": lambda: "JP",
    "quota_limit": lambda: str(quota.DEFAULT_LIMIT),
    "quota_margin": lambda: str(quota.DEFAULT_MARGIN),
    "include_protected": lambda: "0",
    "auto_resume": lambda: "1",
}


def setting(conn: sqlite3.Connection, key: str) -> str:
    return db.get_setting(conn, key) or SETTING_DEFAULTS[key]()


def quota_dict(conn: sqlite3.Connection) -> dict:
    st = quota.status(conn)
    return {
        "used": st.used, "limit": st.limit, "margin": st.margin, "available": st.available,
        "inserts_possible": st.inserts_possible, "reset_at": st.reset_at.isoformat(),
        "breakdown": quota.breakdown(conn),
    }


@router.get("/status")
def status(conn: sqlite3.Connection = Depends(get_conn), runner: JobRunner = Depends(get_runner)):
    lazer = Path(setting(conn, "lazer_path"))
    try:
        auth.check_client_secret()
        secret_ok, secret_msg = True, None
    except auth.AuthError as e:
        secret_ok, secret_msg = False, str(e)
    try:
        creds = auth.load_credentials()
        logged_in, auth_msg = bool(creds and creds.valid), None
    except auth.AuthError as e:
        logged_in, auth_msg = False, str(e)
    active = playlists.active(conn)
    last_scan = conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    job = runner.current()
    return {
        "env": {
            "lazer_path": str(lazer),
            "realm_found": (lazer / "client.realm").is_file(),
            "dotnet": helper.dotnet_path(),
            "helper_built": helper.exe_path().exists(),
            "ffprobe": audio.ffprobe_path(),
            "yt_dlp": yt_dlp.version.__version__,
        },
        "auth": {"client_secret": secret_ok, "client_secret_message": secret_msg,
                 "logged_in": logged_in, "message": auth_msg},
        "playlist": dict(active) if active else None,
        "quota": quota_dict(conn),
        "last_scan": dict(last_scan) if last_scan else None,
        "job": job.to_dict(log_tail=0) if job else None,
    }


# --- settings -------------------------------------------------------------------------------------

class Settings(BaseModel):
    lazer_path: str | None = None
    region: str | None = Field(None, pattern=r"^[A-Z]{2}$")
    quota_limit: int | None = Field(None, ge=100, le=1_000_000)
    quota_margin: int | None = Field(None, ge=0, le=100_000)
    include_protected: bool | None = None
    auto_resume: bool | None = None


@router.get("/settings")
def get_settings(conn: sqlite3.Connection = Depends(get_conn)):
    return {k: setting(conn, k) for k in SETTING_DEFAULTS}


@router.put("/settings")
def put_settings(body: Settings, conn: sqlite3.Connection = Depends(get_conn)):
    values = body.model_dump(exclude_none=True)
    if "lazer_path" in values:
        p = Path(values["lazer_path"]).expanduser()
        if not (p / "client.realm").is_file():
            raise HTTPException(400, f"{p} に client.realm が見つかりません")
        values["lazer_path"] = str(p)
    for k, v in values.items():
        db.set_setting(conn, k, str(int(v)) if isinstance(v, bool) else str(v))
    return get_settings(conn)


# --- jobs ---------------------------------------------------------------------------------------------

@router.get("/jobs/current")
def current_job(runner: JobRunner = Depends(get_runner)):
    job = runner.current()
    return job.to_dict() if job else None


@router.get("/jobs/{job_id}")
def get_job(job_id: int, runner: JobRunner = Depends(get_runner)):
    job = runner.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    return job.to_dict()


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: int, runner: JobRunner = Depends(get_runner)):
    job = runner.get(job_id)
    if not job:
        raise HTTPException(404, "job not found")
    job.cancel.set()
    job.line("中断を要求しました")
    return job.to_dict()


# --- scan / match ---------------------------------------------------------------------------------

class ScanRequest(BaseModel):
    force: bool = False


def scan_job(force: bool):
    def run(job: Job, conn: sqlite3.Connection):
        lazer = Path(setting(conn, "lazer_path"))
        stages = {"copy": "client.realm をコピー中", "export": "ヘルパーで読み取り中", "audio": "音声の長さを取得中"}

        def progress(stage, done, total):
            job.progress(done, total, stages.get(stage, stage))

        if helper.needs_build():
            job.progress(0, 0, ".NET ヘルパーをビルド中 (初回のみ)")
            job.line(".NET ヘルパーをビルドしています")
            helper.build()
        r = scan(conn, lazer, config.WORK_DIR, force=force,
                 include_protected=setting(conn, "include_protected") == "1", progress=progress)
        if r.status == "skipped":
            job.line("client.realm は前回から変更なし → スキップ")
        else:
            job.line(f"{r.total} セット / 新規 {r.new} / 消えた {r.gone} / 復活 {r.returned}")
        for w in r.warnings:
            job.line(f"警告: {w}")
        return {"status": r.status, "total": r.total, "new": r.new, "gone": r.gone, "returned": r.returned,
                "warnings": r.warnings}
    return run


@router.post("/library/scan")
def start_scan(body: ScanRequest, runner: JobRunner = Depends(get_runner)):
    return start_job(runner, "scan", "ライブラリのスキャン", scan_job(body.force))


class MatchRequest(BaseModel):
    scope: Literal["unmatched", "needs_review", "auto", "keys"] = "unmatched"
    keys: list[str] | None = None
    workers: int = Field(3, ge=1, le=6)


def match_job(scope: str, keys: list[str] | None, workers: int):
    def run(job: Job, conn: sqlite3.Connection):
        rows = matcher.select_rows(conn, scope, keys)
        job.progress(0, len(rows), "YouTube を検索中")
        if not rows:
            job.line("対象の曲がありません")
            return {"total": 0}

        def progress(done, total, o):
            job.progress(done, total)
            if o.error and o.error != "cancelled":
                job.line(f"失敗 {o.key}: {o.error[:100]}")
            elif o.decision and o.decision.status != "confirmed":
                job.line(f"要確認 {o.key} ({o.decision.reason})")

        s = matcher.run_matching(conn, rows, workers=workers, progress=progress, cancel=job.cancel)
        job.line(f"確定 {s.confirmed} / 要確認 {s.needs_review} / 失敗 {s.failed}")
        return {"total": s.total, "confirmed": s.confirmed, "needs_review": s.needs_review, "failed": s.failed}
    return run


@router.post("/match")
def start_match(body: MatchRequest, runner: JobRunner = Depends(get_runner)):
    if body.scope == "keys" and not body.keys:
        raise HTTPException(400, "keys を指定してください")
    return start_job(runner, "match", "YouTube マッチング", match_job(body.scope, body.keys, body.workers))


@router.post("/match/rescore")
def rescore(conn: sqlite3.Connection = Depends(get_conn)):
    s = matcher.rescore(conn, matcher.select_rows(conn, "auto"))
    return {"total": s.total, "confirmed": s.confirmed, "needs_review": s.needs_review}


@router.get("/quota")
def get_quota(conn: sqlite3.Connection = Depends(get_conn)):
    return quota_dict(conn)
