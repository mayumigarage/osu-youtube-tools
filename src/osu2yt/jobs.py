"""Background jobs (scan, matching, sync): one at a time, progress polled by the WebUI."""

from __future__ import annotations

import itertools
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import db
from .db import now_iso
from .logging_setup import mask

log = logging.getLogger(__name__)

_ids = itertools.count(1)


class JobBusy(RuntimeError):
    pass


@dataclass
class Job:
    kind: str
    label: str
    id: int = field(default_factory=lambda: next(_ids))
    status: str = "running"        # running / done / error / cancelled
    done: int = 0
    total: int = 0
    message: str = ""
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    result: dict | None = None
    error: str | None = None
    log: deque = field(default_factory=lambda: deque(maxlen=300))
    cancel: threading.Event = field(default_factory=threading.Event)

    def progress(self, done: int, total: int, message: str | None = None) -> None:
        self.done, self.total = done, total
        if message is not None:
            self.message = message

    def line(self, text: str) -> None:
        self.log.append(mask(text))

    def to_dict(self, log_tail: int = 50) -> dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "done": self.done, "total": self.total, "message": self.message,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "result": self.result, "error": self.error,
            "log": list(self.log)[-log_tail:] if log_tail else [],
            "cancel_requested": self.cancel.is_set(),
        }


JobFn = Callable[[Job, Any], "dict | None"]  # (job, sqlite connection) -> result


class JobRunner:
    def __init__(self, db_path: Path | str):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._current: Job | None = None
        self._last: Job | None = None

    def start(self, kind: str, label: str, fn: JobFn) -> Job:
        with self._lock:
            if self._current and self._current.status == "running":
                raise JobBusy(f"「{self._current.label}」を実行中です。終わってから再実行してください。")
            job = Job(kind, label)
            self._jobs[job.id] = job
            self._current = job
        threading.Thread(target=self._run, args=(job, fn), name=f"job-{kind}-{job.id}", daemon=True).start()
        return job

    def _run(self, job: Job, fn: JobFn) -> None:
        conn = db.connect(self.db_path)
        try:
            job.result = fn(job, conn) or {}
            job.status = "cancelled" if job.cancel.is_set() else "done"
        except Exception as e:  # noqa: BLE001 - surfaced to the UI
            log.exception("job %s failed", job.kind)
            job.status, job.error = "error", mask(str(e))
        finally:
            job.finished_at = now_iso()
            conn.close()
            with self._lock:
                self._last = job
                if self._current is job:
                    self._current = None

    def get(self, job_id: int) -> Job | None:
        return self._jobs.get(job_id)

    def current(self) -> Job | None:
        """The running job, else the most recently finished one."""
        return self._current or self._last

    def running(self) -> Job | None:
        return self._current

    def wait(self, job: Job, timeout: float = 10) -> None:
        """Test helper: block until a job finishes."""
        for t in threading.enumerate():
            if t.name == f"job-{job.kind}-{job.id}":
                t.join(timeout)
