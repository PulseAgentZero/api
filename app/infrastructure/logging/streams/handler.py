"""Logging handler that forwards records to the global log stream manager."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.infrastructure.logging.streams.manager import LogStreamManager

_manager: "LogStreamManager | None" = None

_SELF_LOGGER_PREFIXES = (
    "app.infrastructure.logging.streams",
    "pulse.logstream.",
)


def set_stream_manager(manager: "LogStreamManager") -> None:
    global _manager
    _manager = manager


def get_stream_manager() -> "LogStreamManager | None":
    return _manager


class StreamingHandler(logging.Handler):
    """Non-blocking handler — enqueues formatted records for async delivery.

    Skips records originating inside the log-stream pipeline itself to prevent
    runaway recursion when a destination is misconfigured and the manager logs
    its own failures.
    """

    def emit(self, record: logging.LogRecord) -> None:
        if _manager is None:
            return
        if getattr(record, "_no_stream", False):
            return
        name = record.name or ""
        if any(name.startswith(p) for p in _SELF_LOGGER_PREFIXES):
            return
        try:
            _manager.handle_record(record)
        except Exception:
            self.handleError(record)
