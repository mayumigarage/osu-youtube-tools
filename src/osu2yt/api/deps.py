"""Shared FastAPI dependencies."""

from __future__ import annotations

import sqlite3
from typing import Iterator

from fastapi import HTTPException, Request

from .. import db
from ..jobs import JobBusy, JobRunner


def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


def get_runner(request: Request) -> JobRunner:
    return request.app.state.runner


def start_job(runner: JobRunner, kind: str, label: str, fn) -> dict:
    try:
        return runner.start(kind, label, fn).to_dict()
    except JobBusy as e:
        raise HTTPException(409, str(e)) from e


def bad_request(e: Exception) -> HTTPException:
    return HTTPException(400, str(e))
