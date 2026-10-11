"""Thin data-access helpers for the identity DB.

Repositories do NO permission checks — services enforce them. This keeps SQL
in one place so a future PostgreSQL migration touches only these functions.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.errors import AlreadyExists, NotFound
from app.identity import models as m


# --- companies ---
def add_company(s: Session, **kw) -> m.Company:
    if s.query(m.Company).filter_by(name_en=kw["name_en"]).first():
        raise AlreadyExists(f"company '{kw['name_en']}' already exists")
    c = m.Company(**kw)
    s.add(c)
    s.flush()
    return c


def get_company(s: Session, company_id: int) -> m.Company:
    c = s.get(m.Company, company_id)
    if c is None:
        raise NotFound("company not found")
    return c


# --- offices ---
def add_office(s: Session, **kw) -> m.Office:
    o = m.Office(**kw)
    s.add(o)
    s.flush()
    return o


def get_office(s: Session, office_id: int) -> m.Office:
    o = s.get(m.Office, office_id)
    if o is None:
        raise NotFound("office not found")
    return o


# --- users ---
def add_user(s: Session, **kw) -> m.User:
    if s.query(m.User).filter_by(phone=kw["phone"]).first():
        raise AlreadyExists(f"phone '{kw['phone']}' already registered")
    u = m.User(**kw)
    s.add(u)
    s.flush()
    return u


def get_user(s: Session, user_id: int) -> m.User:
    u = s.get(m.User, user_id)
    if u is None:
        raise NotFound("user not found")
    return u


def get_user_by_phone(s: Session, phone: str) -> m.User | None:
    return s.query(m.User).filter_by(phone=phone).first()


# --- memberships ---
def add_membership(s: Session, **kw) -> m.Membership:
    mship = m.Membership(**kw)
    s.add(mship)
    s.flush()
    return mship


def end_membership(s: Session, membership_id: int, ended_at) -> m.Membership:
    mship = s.get(m.Membership, membership_id)
    if mship is None:
        raise NotFound("membership not found")
    mship.status = "ended"
    mship.ended_at = ended_at
    s.flush()
    return mship


# --- audit ---
def log_central_event(s: Session, **kw) -> m.CentralAuditEvent:
    e = m.CentralAuditEvent(**kw)
    s.add(e)
    s.flush()
    return e
