from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from .services.job_observability import redact_log_text


class RedactingJsonFormatter(logging.Formatter):
    """Emit a small structured record without serializing arbitrary LogRecord state."""

    _extra_fields = (
        "request_id",
        "method",
        "path",
        "status_code",
        "duration_ms",
        "job_id",
        "attempt_id",
        "worker_id",
    )

    def format(self, record: logging.LogRecord) -> str:
        document: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": redact_log_text(record.getMessage()),
        }
        for field in self._extra_fields:
            value = getattr(record, field, None)
            if isinstance(value, (str, int, float, bool)):
                document[field] = redact_log_text(value) if isinstance(value, str) else value
        if record.exc_info:
            document["exception"] = redact_log_text(self.formatException(record.exc_info))
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def configure_logging(level: str = "INFO") -> None:
    """Install the production formatter once for API and Uvicorn application logs."""

    normalized = level.upper()
    numeric_level = getattr(logging, normalized, logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingJsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(numeric_level)
    for logger_name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(logger_name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True
    # The Manager middleware is the only access logger. Uvicorn's access record
    # includes the raw query string, which may contain signed storage credentials.
    logging.getLogger("uvicorn.access").disabled = True
