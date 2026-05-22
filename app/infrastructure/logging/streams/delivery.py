"""Deliver batched log records to HTTP, syslog, or file destinations."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import logging.handlers
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _min_level_no(name: str) -> int:
    return _LEVEL_MAP.get((name or "INFO").upper(), logging.INFO)


async def deliver_http_batch(
    config: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    timeout_s: float = 10.0,
) -> tuple[bool, str | None]:
    url = str(config.get("url") or "").strip()
    if not url:
        return False, "Missing url"
    method = str(config.get("method") or "POST").upper()
    headers = dict(config.get("headers") or {})
    headers.setdefault("Content-Type", "application/json")
    body = json.dumps({"records": records, "count": len(records)}, default=str).encode()
    secret = config.get("hmac_secret")
    if secret:
        sig = hmac.new(str(secret).encode(), body, hashlib.sha256).hexdigest()
        headers["X-Pulse-Signature"] = f"sha256={sig}"
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.request(method, url, content=body, headers=headers)
            if resp.status_code >= 400:
                return False, f"HTTP {resp.status_code}: {(resp.text or '')[:500]}"
            return True, None
    except Exception as e:
        return False, str(e)[:2000]


def deliver_syslog_batch(config: dict[str, Any], records: list[dict[str, Any]]) -> tuple[bool, str | None]:
    host = str(config.get("host") or "").strip()
    port = int(config.get("port") or 514)
    protocol = str(config.get("protocol") or "udp").lower()
    app_name = str(config.get("app_name") or "pulse")
    facility = int(config.get("facility") or logging.handlers.SysLogHandler.LOG_USER)
    handler: logging.handlers.SysLogHandler | None = None
    try:
        handler = logging.handlers.SysLogHandler(
            address=(host, port),
            facility=facility,
            socktype=socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM,
        )
        if protocol == "tls":
            handler.socket = ssl.create_default_context().wrap_socket(
                socket.create_connection((host, port)),
                server_hostname=host,
            )
        syslog_logger = logging.getLogger(f"pulse.logstream.syslog.{host}:{port}")
        syslog_logger.handlers = [handler]
        syslog_logger.propagate = False
        for rec in records:
            level = (rec.get("level") or "INFO").upper()
            msg = json.dumps(rec, default=str, ensure_ascii=False)
            syslog_logger.log(_min_level_no(level), "%s %s", app_name, msg)
        return True, None
    except Exception as e:
        return False, str(e)[:2000]
    finally:
        if handler is not None:
            try:
                handler.close()
            except Exception:  # noqa: BLE001
                pass


# Cache of (path, max_bytes, backup_count) → configured logger so we only attach
# one RotatingFileHandler per destination but still rotate config changes safely.
_FILE_LOGGERS: dict[str, tuple[tuple[str, int, int], logging.Logger]] = {}


def _file_logger(path: Path, max_bytes: int, backup_count: int) -> logging.Logger:
    name = f"pulse.logstream.file.{path}"
    sig = (str(path), max_bytes, backup_count)
    cached = _FILE_LOGGERS.get(name)
    if cached and cached[0] == sig:
        return cached[1]
    file_logger = logging.getLogger(name)
    for h in list(file_logger.handlers):
        try:
            h.close()
        except Exception:  # noqa: BLE001
            pass
        file_logger.removeHandler(h)
    fh = logging.handlers.RotatingFileHandler(
        str(path),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    fh.setFormatter(logging.Formatter("%(message)s"))
    file_logger.addHandler(fh)
    file_logger.propagate = False
    file_logger.setLevel(logging.DEBUG)
    _FILE_LOGGERS[name] = (sig, file_logger)
    return file_logger


def deliver_file_batch(config: dict[str, Any], records: list[dict[str, Any]]) -> tuple[bool, str | None]:
    path = Path(str(config.get("path") or "/var/log/pulse/stream.log"))
    max_bytes = int(config.get("max_bytes") or 10_485_760)
    backup_count = int(config.get("backup_count") or 3)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file_logger = _file_logger(path, max_bytes, backup_count)
        for rec in records:
            file_logger.info(json.dumps(rec, default=str, ensure_ascii=False))
        return True, None
    except Exception as e:
        return False, str(e)[:2000]


async def deliver_batch(
    destination_type: str,
    config: dict[str, Any],
    records: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    if not records:
        return True, None
    if destination_type == "http":
        return await deliver_http_batch(config, records)
    if destination_type == "syslog":
        return deliver_syslog_batch(config, records)
    if destination_type == "file":
        return deliver_file_batch(config, records)
    return False, f"Unknown destination_type: {destination_type}"
