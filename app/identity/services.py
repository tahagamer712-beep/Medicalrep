"""Identity business logic. Every function enforces permissions first."""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from app.core import idempotency
from app.core.errors import NotFound, ValidationError
from app.core.permissions import require_owner, require_permission
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.identity import models as m
from app.identity import repositories as r


def _audit(s: Session, *, actor, event_type, result="ok", company_id=None,
           office_id=None, entity=None, entity_id=None, reason=None, meta=None):
    r.log_central_event(
        s, actor_user_id=actor, event_type=event_type, result=result,
        company_id=company_id, office_id=office_id, entity=entity,
        entity_id=entity_id, reason=reason,
        request_id=idempotency.new_uuid(), meta_json=meta,
    )


def create_company(s: Session, actor_user_id: int, *, name_ar: str, name_en: str,
                   timezone: str = "Asia/Baghdad") -> m.Company:
    require_owner(s, actor_user_id)
    if not name_ar.strip() or not name_en.strip():
        raise ValidationError("company names are required")
    c = r.add_company(s, name_ar=name_ar.strip(), name_en=name_en.strip(),
                      timezone=timezone)
    _audit(s, actor=actor_user_id, event_type="OWNER_CREATED_COMPANY",
           company_id=c.id, entity="company", entity_id=c.id)
    return c


def create_office(s: Session, data_dir: Path, actor_user_id: int, *,
                  company_id: int, name_ar: str, name_en: str, city: str = "",
                  area: str = "", timezone: str = "Asia/Baghdad") -> m.Office:
    """Create office + provision its database. Atomic-ish: on provisioning
    failure the office row is rolled back and no orphan DB file remains."""
    require_owner(s, actor_user_id)
    company = r.get_company(s, company_id)
    if company.status != "active":
        raise ValidationError("company is not active")
    office = r.add_office(
        s, company_id=company.id, name_ar=name_ar.strip(), name_en=name_en.strip(),
        city=city, area=area, timezone=timezone,
    )
    s.flush()  # get office.id
    try:
        db_path = dbm.provision_office_db(data_dir, office.id)
    except Exception as exc:
        s.rollback()
        # remove the half-created file if the provisioner left one
        orphan = dbm.office_db_path(data_dir, office.id)
        if orphan.exists():
            orphan.unlink()
        _audit(s, actor=actor_user_id, event_type="OFFICE_CREATE_FAILED",
               result="failed", company_id=company.id, reason=str(exc))
        s.commit()
        raise
    office.db_path = str(db_path)
    _audit(s, actor=actor_user_id, event_type="OWNER_CREATED_OFFICE",
           company_id=company.id, office_id=office.id,
           entity="office", entity_id=office.id)
    return office


def create_user(s: Session, actor_user_id: int, *, full_name: str, phone: str,
                telegram_id: int | None = None,
                company_id: int | None = None) -> m.User:
    if company_id is not None:
        require_permission(s, actor_user_id, "user.create", company_id=company_id)
    else:
        require_owner(s, actor_user_id)
    if not full_name.strip() or not phone.strip():
        raise ValidationError("name and phone are required")
    u = r.add_user(s, full_name=full_name.strip(), phone=phone.strip(),
                   telegram_id=telegram_id)
    _audit(s, actor=actor_user_id, event_type="USER_CREATED",
           company_id=company_id, entity="user", entity_id=u.id)
    return u


def assign_membership(s: Session, actor_user_id: int, *, user_id: int,
                      company_id: int | None, office_id: int | None,
                      role: str) -> m.Membership:
    if role == "owner":
        require_owner(s, actor_user_id)
    elif office_id is not None:
        require_permission(s, actor_user_id, "rep.manage", office_id=office_id)
    elif company_id is not None:
        require_permission(s, actor_user_id, "user.create", company_id=company_id)
    else:
        require_owner(s, actor_user_id)
    if role not in ("owner", "manager", "supervisor", "rep"):
        raise ValidationError(f"unknown role '{role}'")
    r.get_user(s, user_id)  # raises NotFound if missing
    mship = r.add_membership(s, user_id=user_id, company_id=company_id,
                             office_id=office_id, role=role)
    _audit(s, actor=actor_user_id, event_type="MEMBERSHIP_ASSIGNED",
           company_id=company_id, office_id=office_id,
           entity="membership", entity_id=mship.id,
           meta=f'{{"role":"{role}","user_id":{user_id}}}')
    return mship


def end_membership(s: Session, actor_user_id: int, membership_id: int,
                   reason: str = "") -> m.Membership:
    mship = s.get(m.Membership, membership_id)
    if mship is None:
        raise NotFound("membership not found")
    if mship.office_id is not None:
        require_permission(s, actor_user_id, "rep.manage", office_id=mship.office_id)
    elif mship.company_id is not None:
        require_permission(s, actor_user_id, "user.create", company_id=mship.company_id)
    else:
        require_owner(s, actor_user_id)
    r.end_membership(s, membership_id, utcnow())
    _audit(s, actor=actor_user_id, event_type="MEMBERSHIP_ENDED",
           company_id=mship.company_id, office_id=mship.office_id,
           entity="membership", entity_id=mship.id, reason=reason)
    return mship
