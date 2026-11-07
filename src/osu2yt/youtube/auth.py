"""OAuth 2.0 for an installed (desktop) app with a loopback redirect.

- client_secret.json: created by the user in Google Cloud (OAuth client type "Desktop app"),
  placed at data/client_secret.json.
- token.json: written to data/ (git-ignored). Never printed or logged.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .. import config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube"]


class AuthError(RuntimeError):
    """User-facing (Japanese) authentication problem."""


def client_secret_path() -> Path:
    return config.DATA_DIR / "client_secret.json"


def token_path() -> Path:
    return config.DATA_DIR / "token.json"


def check_client_secret(path: Path | None = None) -> None:
    path = path or client_secret_path()
    if not path.is_file():
        raise AuthError(
            f"{path} がありません。README の「Google Cloud の設定」に沿って OAuth クライアント "
            "(種類: デスクトップアプリ) を作成し、JSON をこの名前で保存してください。"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise AuthError(f"client_secret.json を読み込めません: {e}") from e
    if "installed" not in data:
        kind = next(iter(data), "?")
        raise AuthError(
            f"client_secret.json の種類が「{kind}」です。OAuth クライアントは「デスクトップアプリ」で作成してください。"
        )


def _write_token(creds: Credentials) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(creds.to_json(), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)  # best effort; mostly meaningful on POSIX
    except OSError:
        pass
    tmp.replace(path)


def load_credentials(refresh: bool = True) -> Credentials | None:
    """Saved credentials, refreshed if needed. None when not logged in."""
    path = token_path()
    if not path.is_file():
        return None
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    except (ValueError, OSError):
        log.warning("token.json is unreadable; please log in again")
        return None
    if refresh and not creds.valid and creds.refresh_token:
        try:
            creds.refresh(Request())
        except RefreshError as e:
            # Typical cause: consent screen in "Testing" status -> refresh tokens expire after 7 days.
            path.unlink(missing_ok=True)
            raise AuthError(
                "ログインの有効期限が切れました。再ログインしてください "
                "(OAuth 同意画面が「テスト」状態だと7日で失効します)。"
            ) from e
        _write_token(creds)
    return creds


def require_credentials() -> Credentials:
    creds = load_credentials()
    if creds is None or not creds.valid:
        raise AuthError("YouTube にログインしていません。`python -m osu2yt auth login` を実行してください。")
    return creds


def login_interactive(open_browser: bool = True) -> Credentials:
    """CLI login: temporary loopback server on a free port, opens the browser."""
    check_client_secret()
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path()), SCOPES)
    creds = flow.run_local_server(
        host="127.0.0.1", port=0, open_browser=open_browser,
        authorization_prompt_message="ブラウザで Google アカウントを選んで許可してください:\n{url}",
        success_message="認証が完了しました。このタブを閉じて osu2yt に戻ってください。",
    )
    _write_token(creds)
    return creds


def logout() -> bool:
    """Delete the local token (and try to revoke it). Returns True if a token existed."""
    path = token_path()
    if not path.is_file():
        return False
    try:
        creds = Credentials.from_authorized_user_file(str(path), SCOPES)
        token = creds.refresh_token or creds.token
        if token:
            import requests

            requests.post("https://oauth2.googleapis.com/revoke", data={"token": token}, timeout=10)
    except Exception:  # revocation is best effort; never log the token
        log.info("token revocation skipped")
    path.unlink(missing_ok=True)
    return True
