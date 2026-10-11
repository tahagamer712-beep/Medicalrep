"""Central identity database models (data/identity.db).

Stores ONLY identity/routing data: companies, offices, users, memberships,
and central audit events. No operational data (visits, payroll, plans) here.
"""
from datetime import datetime

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.core.time_utils import utcnow
from app.core.types import UTCDateTime


class IdentityBase(DeclarativeBase):
    pass


class Company(IdentityBase):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name_ar: Mapped[str] = mapped_column(String(200))
    name_en: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|suspended
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Baghdad")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Office(IdentityBase):
    __tablename__ = "offices"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name_ar: Mapped[str] = mapped_column(String(200))
    name_en: Mapped[str] = mapped_column(String(200))
    city: Mapped[str] = mapped_column(String(100), default="")
    area: Mapped[str] = mapped_column(String(100), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="Asia/Baghdad")
    work_start: Mapped[str] = mapped_column(String(5), default="08:00")
    work_end: Mapped[str] = mapped_column(String(5), default="16:00")
    work_days: Mapped[str] = mapped_column(Text, default="[6,0,1,2,3]")  # JSON list
    db_path: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class User(IdentityBase):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    full_name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(32), unique=True)
    telegram_id: Mapped[int | None] = mapped_column(unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|disabled
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Membership(IdentityBase):
    """A user's role inside a company/office. History is preserved:
    ending a membership sets ended_at/status instead of deleting the row."""

    __tablename__ = "memberships"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id"), nullable=True, index=True
    )  # NULL = global (owner)
    office_id: Mapped[int | None] = mapped_column(
        ForeignKey("offices.id"), nullable=True, index=True
    )  # NULL = company-level or global
    role: Mapped[str] = mapped_column(String(20))  # owner|manager|supervisor|rep
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|ended
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class CentralAuditEvent(IdentityBase):
    __tablename__ = "central_audit_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    company_id: Mapped[int | None] = mapped_column(nullable=True)
    office_id: Mapped[int | None] = mapped_column(nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[int | None] = mapped_column(nullable=True)
    result: Mapped[str] = mapped_column(String(16))  # ok|denied|failed
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    meta_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class WabaProcessedMessage(IdentityBase):
    """Dedupe ledger for WhatsApp Cloud API inbound messages.

    Meta may redeliver the same webhook event; waba_message_id is globally
    unique per message, so claiming it atomically (INSERT ... ON CONFLICT
    DO NOTHING) guarantees exactly-once processing.
    """
    __tablename__ = "waba_processed_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    waba_message_id: Mapped[str] = mapped_column(String(128), unique=True,
                                                index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"),
                                               nullable=True, index=True)
    wa_phone: Mapped[str] = mapped_column(String(32), default="")
    received_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow,
                                                 index=True)
