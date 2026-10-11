"""Audit helpers for office-level events."""
from sqlalchemy.orm import Session

from app.core import idempotency
from app.core.time_utils import utcnow
from app.databases import office_models as om


def log_office_event(s: Session, *, actor_user_id, event_type, result="ok",
                     entity=None, entity_id=None, reason=None, meta=None):
    e = om.OfficeAuditEvent(
        occurred_at=utcnow(), actor_user_id=actor_user_id,
        event_type=event_type, entity=entity, entity_id=entity_id,
        result=result, reason=reason, request_id=idempotency.new_uuid(),
        meta_json=meta,
    )
    s.add(e)
    s.flush()
    return e
