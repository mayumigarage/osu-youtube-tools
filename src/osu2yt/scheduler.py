"""Auto-resume: while the server runs, restart a sync that was paused by quota once the PT day rolls over."""

from __future__ import annotations

import logging
import threading

from . import db, sync
from .jobs import Job, JobBusy, JobRunner
from .youtube import auth, playlists, quota

log = logging.getLogger(__name__)

RESERVE = sync.INSERT_UNITS + 5   # pull + check + at least one insert


class AutoResume:
    def __init__(self, db_path, runner: JobRunner, interval: float = 300, first_delay: float = 30):
        self.db_path, self.runner = db_path, runner
        self.interval, self.first_delay = interval, first_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="auto-resume", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        delay = self.first_delay
        while not self._stop.wait(delay):
            delay = self.interval
            try:
                self.tick()
            except Exception:  # noqa: BLE001 - never let the loop die
                log.exception("auto-resume check failed")

    def tick(self) -> Job | None:
        """Start the resumed sync if one is due. Returns the started job, if any."""
        from .api.core import setting
        from .api.sync import label, make_sync_job

        if self.runner.running():
            return None
        conn = db.connect(self.db_path)
        try:
            if setting(conn, "auto_resume") != "1":
                return None
            active = playlists.active(conn)
            due = sync.resume_due(conn, active["id"] if active else None)
            if not due or not quota.can_spend(conn, RESERVE):
                return None
            try:
                creds = auth.load_credentials()
            except auth.AuthError as e:
                log.warning("auto-resume skipped: %s", e)
                return None
            if not creds or not creds.valid:
                return None
        finally:
            conn.close()
        try:
            job = self.runner.start("sync", label(False, "auto_resume"), make_sync_job(False, None, "auto_resume"))
        except JobBusy:
            return None
        log.info("auto-resume: restarted sync paused in run #%s", due["id"])
        return job
