"""Diff sync: preview (local, free), run as a background job, history."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .. import sync
from ..jobs import Job, JobRunner
from ..youtube import auth, playlists
from ..youtube.client import YouTubeClient
from .core import setting
from .deps import get_conn, get_runner, start_job

router = APIRouter(prefix="/api/sync")

STAGES = {"pull": "プレイリストの中身を取得中", "check": "動画の生存確認中", "insert": "プレイリストに追加中"}
PREVIEW_ITEMS = 500


def make_sync_job(dry_run: bool, max_add: int | None, trigger: str = "manual"):
    def run(job: Job, conn: sqlite3.Connection):
        active = playlists.active(conn)
        if not active:
            raise sync.SyncError("同期先のプレイリストが選ばれていません。設定で選んでください。")
        client = YouTubeClient.from_saved_credentials(conn)
        r = sync.run_sync(
            conn, client, active["id"], dry_run=dry_run, max_add=max_add,
            region=setting(conn, "region"), trigger=trigger,
            progress=lambda stage, done, total: job.progress(done, total, STAGES.get(stage, stage)),
            cancel=job.cancel, note=job.line,
        )
        job.line(f"結果: 追加 {r.added} / 重複スキップ {r.skipped_dup} / 再生不可 {r.unavailable} / "
                 f"失敗 {r.failed} / 残り {r.remaining} (消費 {r.quota_used} unit)")
        return r.to_dict()
    return run


def label(dry_run: bool, trigger: str = "manual") -> str:
    if dry_run:
        return "同期 (ドライラン)"
    return "同期の自動再開" if trigger == "auto_resume" else "プレイリストへ同期"


@router.get("/preview")
def preview(max_add: int | None = Query(None, ge=1), conn: sqlite3.Connection = Depends(get_conn)):
    active = playlists.active(conn)
    p = sync.plan(conn, active["id"] if active else None)
    try:
        logged_in = bool((c := auth.load_credentials()) and c.valid)
    except auth.AuthError:
        logged_in = False
    due = sync.resume_due(conn, active["id"] if active else None)
    last = conn.execute("SELECT * FROM sync_runs WHERE dry_run=0 ORDER BY id DESC LIMIT 1").fetchone()
    return {
        "playlist": dict(active) if active else None,
        "logged_in": logged_in,
        "summary": {
            "confirmed_sets": p.confirmed_sets, "unique_videos": p.unique_videos, "to_add": len(p.to_add),
            "already_present": p.already_present, "removed_remote": p.removed_remote,
            "unavailable": p.unavailable, "needs_review": p.needs_review, "pulled_at": p.pulled_at,
        },
        "estimate": sync.estimate(conn, p, max_add),
        "items": [
            {"video_id": it.video_id, "set_key": it.set_key, "set_count": len(it.set_keys),
             "title": it.title, "artist": it.artist, "availability": it.availability}
            for it in p.to_add[:PREVIEW_ITEMS]
        ],
        "last_run": dict(last) if last else None,
        "resume_due": due is not None,
        "auto_resume": setting(conn, "auto_resume") == "1",
    }


class SyncRequest(BaseModel):
    dry_run: bool = False
    max_add: int | None = Field(None, ge=1, le=10_000)


@router.post("")
def start_sync(body: SyncRequest, conn: sqlite3.Connection = Depends(get_conn),
               runner: JobRunner = Depends(get_runner)):
    if not playlists.active(conn):
        raise HTTPException(400, "同期先のプレイリストが選ばれていません。設定で選んでください。")
    try:
        auth.require_credentials()
    except auth.AuthError as e:
        raise HTTPException(401, str(e)) from e
    return start_job(runner, "sync", label(body.dry_run), make_sync_job(body.dry_run, body.max_add))


@router.get("/runs")
def list_runs(limit: int = Query(50, ge=1, le=500), conn: sqlite3.Connection = Depends(get_conn)):
    return sync.runs(conn, limit)


@router.get("/runs/{run_id}")
def get_run(run_id: int, conn: sqlite3.Connection = Depends(get_conn)):
    d = sync.run_detail(conn, run_id)
    if not d:
        raise HTTPException(404, "run not found")
    return d
