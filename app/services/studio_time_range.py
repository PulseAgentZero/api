"""Resolve Grafana-style dashboard time range presets to UTC ISO bounds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

VALID_PRESETS = frozenset({
    "last_15m",
    "last_1h",
    "last_6h",
    "last_24h",
    "last_7d",
    "last_30d",
    "custom",
})

_PRESET_DELTAS: dict[str, timedelta] = {
    "last_15m": timedelta(minutes=15),
    "last_1h": timedelta(hours=1),
    "last_6h": timedelta(hours=6),
    "last_24h": timedelta(hours=24),
    "last_7d": timedelta(days=7),
    "last_30d": timedelta(days=30),
}


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    raw = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def resolve_time_range_bounds(
    time_range: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> tuple[str | None, str | None]:
    """Return (__time_from, __time_to) as UTC ISO strings, or (None, None) if unset."""
    if not time_range:
        return None, None

    now = now or datetime.now(timezone.utc)
    preset = str(time_range.get("preset") or "last_24h").lower()

    if preset == "custom":
        start = _parse_iso(time_range.get("from"))
        end = _parse_iso(time_range.get("to")) or now
        if start is None:
            return None, None
        return start.isoformat(), end.isoformat()

    if preset not in _PRESET_DELTAS:
        preset = "last_24h"

    delta = _PRESET_DELTAS[preset]
    start = now - delta
    return start.isoformat(), now.isoformat()


def merge_dashboard_param_values(
    param_values: dict[str, Any],
    time_range: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge user param_values with resolved __time_from / __time_to."""
    merged = dict(param_values)
    time_from, time_to = resolve_time_range_bounds(time_range)
    if time_from is not None:
        merged["__time_from"] = time_from
    if time_to is not None:
        merged["__time_to"] = time_to
    return merged
