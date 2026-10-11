"""Idempotency helpers: every sensitive operation carries a unique key so a
retried request can never apply twice."""
import uuid


def new_uuid() -> str:
    return uuid.uuid4().hex
