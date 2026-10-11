"""Time handling: store UTC in DB, display in office timezone."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "Asia/Baghdad"


def utcnow() -> datetime:
    """Current time as timezone-aware UTC. Use for ALL stored timestamps."""
    return datetime.now(timezone.utc)


def to_office_tz(dt: datetime, tz_name: str = DEFAULT_TZ) -> datetime:
    """Convert a stored UTC datetime to the office's local time for display."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(ZoneInfo(tz_name))


def ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to aware UTC before storage."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
