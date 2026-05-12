"""Opt-in JSON log formatter for machine-parseable production logs.

Selected via the `LOG_FORMAT` env var (or `settings.LOG_FORMAT`):
- "json" -> structured JSON one line per record
- anything else (default) -> standard human-readable text

Designed to be safe to call multiple times. Uvicorn loggers are also
realigned so request logs flow through the same formatter.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone

_RESERVED_RECORD_ATTRS = {
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
}


class JsonFormatter(logging.Formatter):
    """Minimal JSON log formatter — no external dependency."""

    def format(self, record: logging.LogRecord) -> str:  # noqa: A003
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack_info"] = self.formatStack(record.stack_info)

        # Pull through any `extra=...` fields the caller attached.
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key.startswith("_"):
                continue
            try:
                json.dumps(value, default=str)
                payload[key] = value
            except TypeError:
                payload[key] = repr(value)

        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str | None = None, fmt: str | None = None) -> None:
    """Install the chosen log format on the root logger and uvicorn loggers.

    Idempotent — safe to call multiple times (e.g. on uvicorn reload).
    """
    chosen_fmt = (fmt or os.getenv("LOG_FORMAT", "text")).lower()
    chosen_level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()

    if chosen_fmt == "json":
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    # Replace existing handlers so the formatter is consistent.
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(chosen_level)

    # Uvicorn installs its own handlers — clear them so records bubble up
    # to our root handler with the chosen formatter.
    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(noisy)
        for existing in list(lg.handlers):
            lg.removeHandler(existing)
        lg.propagate = True
