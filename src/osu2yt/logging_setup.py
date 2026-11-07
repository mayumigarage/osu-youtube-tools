"""Logging with secret masking: OAuth tokens and client secrets must never reach a log line."""

from __future__ import annotations

import logging
import re

_SECRET_PATTERNS = [
    re.compile(r"ya29\.[0-9A-Za-z_\-]+"),                 # access tokens
    re.compile(r"1//[0-9A-Za-z_\-]{10,}"),                # refresh tokens
    re.compile(r"GOCSPX-[0-9A-Za-z_\-]+"),                # client secrets
    re.compile(r"(?i)(\"?(?:access_token|refresh_token|client_secret|id_token|code)\"?\s*[:=]\s*\"?)[^\"&\s,}]+"),
    re.compile(r"(?i)(authorization:\s*bearer\s+)\S+"),
]


def mask(text: str) -> str:
    for rx in _SECRET_PATTERNS:
        text = rx.sub(lambda m: (m.group(1) if m.groups() else "") + "***", text)
    return text


class SecretFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:
            return True
        masked = mask(msg)
        if masked != msg:
            record.msg, record.args = masked, None
        return True


def setup(level: int = logging.INFO) -> None:
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretFilter())
    # These libraries log request URLs / bodies at DEBUG; keep them quiet regardless of our level.
    for name in ("googleapiclient", "google_auth_oauthlib", "oauthlib", "requests_oauthlib", "urllib3", "httplib2"):
        logging.getLogger(name).setLevel(logging.WARNING)
