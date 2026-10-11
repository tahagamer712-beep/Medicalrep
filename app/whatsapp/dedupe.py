"""Exactly-once processing of WhatsApp inbound messages.

Meta may redeliver the same webhook event. We claim each waba_message_id
atomically (INSERT ... ON CONFLICT DO NOTHING): the first claim wins and
the message is processed; later claims are duplicates and skipped.
Lives in the identity DB because a WhatsApp number maps to a User.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.identity import models as im


def claim_message(data_dir: Path, waba_message_id: str,
                  user_id: int | None, wa_phone: str,
                  now: datetime | None = None) -> bool:
    """Try to claim a message for processing.

    Returns True if this is the first time we see waba_message_id
    (caller should process it), False if it was already claimed
    (caller must skip it).
    """
    at = now or utcnow()
    with dbm.identity_session(data_dir) as s:
        row = s.execute(
            text(
                "INSERT INTO waba_processed_messages "
                "(waba_message_id, user_id, wa_phone, received_at) "
                "VALUES (:mid, :uid, :phone, :at) "
                "ON CONFLICT(waba_message_id) DO NOTHING "
                "RETURNING id"
            ),
            {"mid": waba_message_id, "uid": user_id,
             "phone": wa_phone, "at": at},
        ).fetchone()
        return row is not None


def was_processed(data_dir: Path, waba_message_id: str) -> bool:
    with dbm.identity_session(data_dir) as s:
        return (s.query(im.WabaProcessedMessage)
                .filter_by(waba_message_id=waba_message_id)
                .first() is not None)


def last_inbound_at(data_dir: Path, user_id: int) -> datetime | None:
    """Most recent inbound message time for a user (24h window tracking)."""
    with dbm.identity_session(data_dir) as s:
        row = (s.query(im.WabaProcessedMessage)
               .filter_by(user_id=user_id)
               .order_by(im.WabaProcessedMessage.received_at.desc())
               .first())
        return row.received_at if row else None
