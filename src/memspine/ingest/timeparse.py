"""Event-time parsing for the write path (F-09)."""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo


def _zone_of(name: str) -> tzinfo:
    """F5: an IANA zone name -> tzinfo (``UTC`` needs no tz database)."""
    if name.upper() == "UTC":
        return UTC
    from zoneinfo import ZoneInfo

    return ZoneInfo(name)


def _parse_event_time(value: object) -> datetime | None:
    """ISO-8601 string or datetime -> aware datetime; None when absent/unparseable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
