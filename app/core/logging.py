"""Logging setup: plain text in development, single-line JSON in production."""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

from app.core.config import settings

_REDACT_KEYS = ("password", "token", "secret", "api_key", "apikey", "authorization")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("request_id", "path", "method", "status", "duration_ms", "actor"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class RedactFilter(logging.Filter):
    """Never let secrets slip into logs through message args."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, dict):
            record.args = {k: ("***" if any(s in k.lower() for s in _REDACT_KEYS) else v) for k, v in record.args.items()}
        return True


def configure_logging() -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.addFilter(RedactFilter())
    if settings.log_json:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
    root.addHandler(handler)
    root.setLevel(settings.log_level.upper())
    for noisy in ("uvicorn.access", "httpx", "httpcore", "telegram", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.INFO if settings.db_echo else logging.WARNING)
