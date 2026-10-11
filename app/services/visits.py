"""Actual visits — strictly separated from planned visits.

- A planned visit NEVER becomes an actual visit automatically. Recording an
  actual visit requires an explicit call with confirm=True.
- Corrections create a NEW row pointing at the old one (correction_of);
  the old row is marked corrected. History is never rewritten.
- visited_at always uses server time unless an explicit, permission-checked
  backdate is supplied (supervisor+ only, logged).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.core.time_utils import ensure_utc, utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services.audit import log_office_event


def _require_self_or_manager(isess: Session, actor: int, office_id: int,
                             rep_user_id: int, record_action: str,
                             correct_action: str) -> None:
    """Supervisors+ (correct_action) may act for anyone; otherwise the actor
    must be the rep themself with record_action."""
    try:
        require_permission(isess, actor, correct_action, office_id=office_id)
        return
    except PermissionDenied:
        pass
    require_permission(isess, actor, record_action, office_id=office_id)
    if actor != rep_user_id:
        raise PermissionDenied("can only record own visits/attendance")


def _require_active_rep(isess: Session, rep_user_id: int, office_id: int) -> None:
    m = (isess.query(im.Membership)
         .filter_by(user_id=rep_user_id, office_id=office_id, role="rep",
                    status="active").first())
    if m is None:
        raise PermissionDenied("representative has no active membership here")


def add_doctor(data_dir: Path, actor: int, *, office_id: int, name: str,
               specialty: str = "", address: str = "", phone: str = "",
               area: str = "") -> dict:
    if not name.strip():
        raise ValidationError("doctor name is required")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "directory.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        d = om.Doctor(name=name.strip(), specialty=specialty, address=address,
                      phone=phone, area=area, created_by=actor)
        osess.add(d)
        osess.flush()
        log_office_event(osess, actor_user_id=actor, event_type="DOCTOR_ADDED",
                         entity="doctor", entity_id=d.id)
        return {"id": d.id, "name": d.name}


def add_pharmacy(data_dir: Path, actor: int, *, office_id: int, name: str,
                 address: str = "", phone: str = "", area: str = "") -> dict:
    if not name.strip():
        raise ValidationError("pharmacy name is required")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "directory.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        p = om.Pharmacy(name=name.strip(), address=address, phone=phone,
                        area=area, created_by=actor)
        osess.add(p)
        osess.flush()
        log_office_event(osess, actor_user_id=actor, event_type="PHARMACY_ADDED",
                         entity="pharmacy", entity_id=p.id)
        return {"id": p.id, "name": p.name}


def record_actual_visit(data_dir: Path, actor: int, *, office_id: int,
                        rep_user_id: int, target_type: str, target_id: int,
                        visit_type: str = "routine", outcome: str = "done",
                        notes: str = "", products: list | None = None,
                        confirm: bool = False,
                        visited_at: datetime | None = None) -> dict:
    """Record an actual visit. confirm=True is REQUIRED — there is no silent
    conversion from a planned visit."""
    if not confirm:
        raise ValidationError("explicit confirmation required to record a visit")
    if target_type not in ("doctor", "pharmacy"):
        raise ValidationError("target_type must be doctor or pharmacy")
    with dbm.identity_session(data_dir) as s:
        _require_self_or_manager(s, actor, office_id, rep_user_id,
                                 "visit.record", "visit.correct")
        _require_active_rep(s, rep_user_id, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        _require_target(osess, target_type, target_id)
        at = ensure_utc(visited_at) if visited_at else utcnow()
        v = om.ActualVisit(
            representative_id=rep_user_id, target_type=target_type,
            target_id=int(target_id), visited_at=at, visit_type=visit_type,
            outcome=outcome, notes=notes,
            products_json=json.dumps(products or [], ensure_ascii=False),
            created_by=actor)
        osess.add(v)
        osess.flush()
        log_office_event(osess, actor_user_id=actor, event_type="VISIT_RECORDED",
                         entity="actual_visit", entity_id=v.id)
        return {"visit_id": v.id, "visited_at": at.isoformat()}


def correct_actual_visit(data_dir: Path, actor: int, *, office_id: int,
                         visit_id: int, reason: str, **changes) -> dict:
    """Correct a visit: new row, old row marked corrected. Reason required."""
    if not reason.strip():
        raise ValidationError("correction reason is required")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "visit.correct", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        old = osess.get(om.ActualVisit, visit_id)
        if old is None:
            raise NotFound("visit not found")
        if old.status == "corrected":
            raise ValidationError("visit was already corrected")
        new = om.ActualVisit(
            representative_id=old.representative_id,
            target_type=changes.get("target_type", old.target_type),
            target_id=int(changes.get("target_id", old.target_id)),
            visited_at=ensure_utc(changes["visited_at"]) if "visited_at" in changes else old.visited_at,
            visit_type=changes.get("visit_type", old.visit_type),
            outcome=changes.get("outcome", old.outcome),
            notes=changes.get("notes", old.notes),
            products_json=changes.get("products_json", old.products_json),
            source=old.source, status="recorded",
            correction_of=old.id, created_by=actor)
        osess.add(new)
        osess.flush()
        old.status = "corrected"
        log_office_event(osess, actor_user_id=actor, event_type="VISIT_CORRECTED",
                         entity="actual_visit", entity_id=new.id, reason=reason,
                         meta=json.dumps({"correction_of": old.id}))
        return {"visit_id": new.id, "correction_of": old.id}


def list_actual_visits(data_dir: Path, actor: int, *, office_id: int,
                       rep_user_id: int | None = None,
                       limit: int = 50) -> list[dict]:
    with dbm.identity_session(data_dir) as s:
        if rep_user_id is not None and rep_user_id != actor:
            require_permission(s, actor, "visit.correct", office_id=office_id)
        else:
            try:
                require_permission(s, actor, "visit.correct", office_id=office_id)
            except PermissionDenied:
                require_permission(s, actor, "visit.record", office_id=office_id)
                rep_user_id = actor
    with dbm.office_session(data_dir, office_id) as osess:
        q = osess.query(om.ActualVisit).order_by(om.ActualVisit.id.desc())
        if rep_user_id is not None:
            q = q.filter_by(representative_id=rep_user_id)
        return [{"visit_id": v.id, "rep": v.representative_id,
                 "target": f"{v.target_type}:{v.target_id}",
                 "visited_at": v.visited_at.isoformat(),
                 "outcome": v.outcome, "status": v.status}
                for v in q.limit(limit).all()]


def _require_target(osess: Session, target_type: str, target_id: int) -> None:
    model = om.Doctor if target_type == "doctor" else om.Pharmacy
    target = osess.get(model, int(target_id))
    if target is None or target.status != "active":
        raise NotFound(f"{target_type} not found or inactive")
