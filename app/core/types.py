"""Custom column types.

UTCDateTime: SQLite drops timezone info on round-trip (verified empirically).
This type normalizes everything: naive UTC in storage, aware UTC in Python.
Storage format is unchanged (naive "YYYY-MM-DD HH:MM:SS.ffffff"), so existing
databases remain compatible — no migration needed for this change.
"""
from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value  # already naive UTC by convention
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
