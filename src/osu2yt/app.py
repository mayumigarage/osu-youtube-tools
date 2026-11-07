"""FastAPI application: JSON API under /api and the static WebUI."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, sync
from .api import core, library, youtube
from .api import sync as sync_api
from .jobs import JobRunner
from .scheduler import AutoResume

WEB_DIR = Path(__file__).parent / "web"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "testserver"}  # testserver: FastAPI TestClient

CSP = (
    "default-src 'self'; img-src 'self' https://i.ytimg.com data:; "
    "frame-src https://www.youtube-nocookie.com; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def _hostname(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("["):  # [::1]:8765
        return value[1:value.find("]")]
    return value.rsplit(":", 1)[0] if ":" in value else value


def create_app(db_path: Path | str | None = None) -> FastAPI:
    app = FastAPI(title="osu2yt", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db_path = db_path or config.DB_PATH
    app.state.runner = JobRunner(app.state.db_path)
    conn = db.connect(app.state.db_path)  # run migrations once up front
    try:
        sync.mark_interrupted(conn)        # a sync cut off by a previous shutdown is re-planned next run
    finally:
        conn.close()
    app.state.auto_resume = AutoResume(app.state.db_path, app.state.runner)  # started by `serve`

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # DNS rebinding: only accept requests addressed to localhost.
        if _hostname(request.headers.get("host", "")) not in LOCAL_HOSTS | {"::1"}:
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        # CSRF: state-changing API calls must carry our header (forces a CORS preflight we never allow)
        # and, if the browser sends an Origin, it must be local.
        if request.url.path.startswith("/api/") and request.method not in ("GET", "HEAD", "OPTIONS"):
            if request.headers.get("x-osu2yt") != "1":
                return JSONResponse({"detail": "missing X-Osu2yt header"}, status_code=403)
            origin = request.headers.get("origin")
            if origin and _hostname(origin.split("://", 1)[-1]) not in LOCAL_HOSTS | {"::1"}:
                return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Content-Security-Policy"] = CSP
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"  # YouTube embeds need a referrer
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(core.router)
    app.include_router(library.router)
    app.include_router(youtube.router)
    app.include_router(sync_api.router)

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    return app
