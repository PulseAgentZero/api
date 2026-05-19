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
from pathlib import Path

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

    stream_handler = logging.StreamHandler(stream=sys.stdout)
    stream_handler.setFormatter(formatter)

    root = logging.getLogger()

    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(stream_handler)
    root.setLevel(chosen_level)

    log_file = os.getenv("LOG_FILE", "logs/pulse.log")
    if log_file and log_file.lower() != "none":
        file_path = Path(log_file)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(file_path), encoding="utf-8")
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    chat_log_file = os.getenv("CHAT_LOG_FILE", "logs/chat.log")
    if chat_log_file and chat_log_file.lower() != "none":
        chat_path = Path(chat_log_file)
        chat_path.parent.mkdir(parents=True, exist_ok=True)
        chat_handler = logging.FileHandler(str(chat_path), encoding="utf-8")
        chat_handler.setFormatter(formatter)
        chat_logger = logging.getLogger("pulse.chat")
        chat_logger.setLevel(chosen_level)
        for existing in list(chat_logger.handlers):
            chat_logger.removeHandler(existing)
        chat_logger.addHandler(chat_handler)
        chat_logger.propagate = True

    for noisy in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(noisy)
        for existing in list(lg.handlers):
            lg.removeHandler(existing)
        lg.propagate = True
