"""Doctor profiles: full field set, TGT products, history, field surveys.

Rules from the requirements:
- No hardcoded lists (areas, specialities, classes, products): all data-driven.
- Class: A | B | Z | A-B | "" (unspecified). Anything else rejected.
- Status: active | inactive | left. Doctors are NEVER deleted; leaving the
  area only changes status, history is kept.
- Similar-name detection warns (no unsafe auto-merge).
- Updates use optimistic concurrency (version): stale writes are conflicts,
  never silent overwrites.
- Every field change is logged in doctor_history.
- Rep assignment changes are logged; past visits/surveys stay linked.
"""
from __future__ import annotations

import difflib
import json
import uuid as uuid_lib
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services.audit import log_office_event

CLASSES = ("", "A", "B", "Z", "A-B")
STATUSES = ("active", "inactive", "left")
WEEKDAYS = set(range(7))  # Monday=0 .. Sunday=6, same as office work_days

TRACKED_FIELDS = (
    "name", "specialty", "address", "phone", "area", "sub_area",
    "nearest_pharmacy_id", "doctor_class", "additional_info",
    "reception_days_json", "non_reception_days_json", "reception_time",
    "doctor_notes", "dealer_info", "assigned_rep_id", "status",
)


def _require_manage(isess: Session, actor: int, office_id: int) -> None:
    require_permission(isess, actor, "directory.manage", office_id=office_id)


def _validate_profile(data: dict) -> None:
    if "doctor_class" in data and data["doctor_class"] not in CLASSES:
        raise ValidationError(f"class must be one of {CLASSES}")
    if "status" in data and data["status"] not in STATUSES:
        raise ValidationError(f"status must be one of {STATUSES}")
    for key in ("reception_days_json", "non_reception_days_json"):
        if key in data:
            try:
                days = json.loads(data[key]) if isinstance(data[key], str) \
                    else data[key]
            except (ValueError, TypeError):
                raise ValidationError(f"{key} must be a JSON list of weekdays")
            if not isinstance(days, list) or any(d not in WEEKDAYS for d in days):
                raise ValidationError(f"{key}: weekdays are 0 (Mon) .. 6 (Sun)")
            data[key] = json.dumps(sorted(set(days)))


def find_similar_doctors(osess: Session, name: str,
                         threshold: float = 0.8) -> list[dict]:
    """Warn about probable duplicates. Never auto-merges."""
    out = []
    needle = name.strip().lower()
    for d in osess.query(om.Doctor).all():
        ratio = difflib.SequenceMatcher(None, needle, d.name.lower()).ratio()
        if ratio >= threshold and d.name.strip().lower() != needle:
            out.append({"id": d.id, "name": d.name,
                        "similarity": round(ratio, 2)})
    return sorted(out, key=lambda x: -x["similarity"])


def create_doctor(data_dir: Path, actor: int, *, office_id: int,
                  name: str, **fields) -> dict:
    """fields: any of TRACKED_FIELDS. Returns warnings for similar names."""
    if not name.strip():
        raise ValidationError("doctor name is required")
    _validate_profile(fields)
    with dbm.identity_session(data_dir) as s:
        _require_manage(s, actor, office_id)
        if fields.get("assigned_rep_id") is not None:
            _require_rep(s, fields["assigned_rep_id"], office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        if fields.get("nearest_pharmacy_id") is not None:
            _require_pharmacy(osess, fields["nearest_pharmacy_id"])
        warnings = find_similar_doctors(osess, name)
        d = om.Doctor(name=name.strip(), created_by=actor,
                      external_code=uuid_lib.uuid4().hex[:16])
        for f in TRACKED_FIELDS:
            if f in fields and f != "name":
                setattr(d, f, fields[f])
        osess.add(d)
        osess.flush()
        _log_history(osess, d.id, actor, "created", "", name.strip())
        log_office_event(osess, actor_user_id=actor, event_type="DOCTOR_ADDED",
                         entity="doctor", entity_id=d.id)
        return {"id": d.id, "external_code": d.external_code,
                "similar_warnings": warnings}


def update_doctor(data_dir: Path, actor: int, *, office_id: int, doctor_id: int,
                  expected_version: int, reason: str = "", **fields) -> dict:
    """Optimistic concurrency: expected_version must match, else conflict."""
    _validate_profile(fields)
    with dbm.identity_session(data_dir) as s:
        _require_manage(s, actor, office_id)
        if fields.get("assigned_rep_id") is not None:
            _require_rep(s, fields["assigned_rep_id"], office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        d = osess.get(om.Doctor, doctor_id)
        if d is None:
            raise NotFound("doctor not found")
        if d.version != expected_version:
            raise ValidationError(
                f"conflict: doctor was modified (version {d.version}, "
                f"you based your edit on {expected_version})")
        if fields.get("nearest_pharmacy_id") is not None:
            _require_pharmacy(osess, fields["nearest_pharmacy_id"])
        for f in TRACKED_FIELDS:
            if f in fields:
                old, new = getattr(d, f), fields[f]
                if str(old) != str(new):
                    _log_history(osess, d.id, actor, f, str(old), str(new),
                                 reason)
                    setattr(d, f, new)
        d.version += 1
        d.updated_at = utcnow()
        log_office_event(osess, actor_user_id=actor,
                         event_type="DOCTOR_UPDATED",
                         entity="doctor", entity_id=d.id, reason=reason)
        return {"id": d.id, "version": d.version}


def set_doctor_status(data_dir: Path, actor: int, *, office_id: int,
                      doctor_id: int, status: str, reason: str = "") -> dict:
    if status not in STATUSES:
        raise ValidationError(f"status must be one of {STATUSES}")
    with dbm.identity_session(data_dir) as s:
        _require_manage(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        d = osess.get(om.Doctor, doctor_id)
        if d is None:
            raise NotFound("doctor not found")
        if d.status != status:
            _log_history(osess, d.id, actor, "status", d.status, status,
                         reason)
            d.status = status
            d.version += 1
            d.updated_at = utcnow()
        return {"id": d.id, "status": d.status, "version": d.version}


def set_tgt_products(data_dir: Path, actor: int, *, office_id: int,
                     doctor_id: int, product_ids: list[int]) -> dict:
    """Replace the TGT product set (linked to the real catalog)."""
    with dbm.identity_session(data_dir) as s:
        _require_manage(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        d = osess.get(om.Doctor, doctor_id)
        if d is None:
            raise NotFound("doctor not found")
        for pid in product_ids:
            p = osess.get(om.Product, pid)
            if p is None or p.status != "active":
                raise NotFound(f"product {pid} not found or inactive")
        current = {dp.product_id for dp in osess.query(om.DoctorProduct)
                   .filter_by(doctor_id=doctor_id).all()}
        wanted = set(product_ids)
        for pid in wanted - current:
            osess.add(om.DoctorProduct(doctor_id=doctor_id, product_id=pid,
                                       added_by=actor))
        for pid in current - wanted:
            (osess.query(om.DoctorProduct)
             .filter_by(doctor_id=doctor_id, product_id=pid).delete())
        _log_history(osess, doctor_id, actor, "tgt_products",
                     ",".join(map(str, sorted(current))),
                     ",".join(map(str, sorted(wanted))))
        d.version += 1
        d.updated_at = utcnow()
        return {"doctor_id": doctor_id, "tgt_products": sorted(wanted)}


def add_field_survey(data_dir: Path, actor: int, *, office_id: int,
                     doctor_id: int, products_prices: list | None = None,
                     competitors: list | None = None,
                     market_notes: str = "") -> dict:
    """Time-varying market data. Append-only; last update shown on profile."""
    with dbm.identity_session(data_dir) as s:
        _require_manage(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        d = osess.get(om.Doctor, doctor_id)
        if d is None:
            raise NotFound("doctor not found")
        fs = om.DoctorFieldSurvey(
            doctor_id=doctor_id, rep_id=actor,
            products_prices_json=json.dumps(products_prices or [],
                                            ensure_ascii=False),
            competitors_json=json.dumps(competitors or [], ensure_ascii=False),
            market_notes=market_notes)
        osess.add(fs)
        osess.flush()
        return {"survey_id": fs.id, "recorded_at": fs.recorded_at.isoformat()}


def get_doctor(data_dir: Path, actor: int, *, office_id: int,
               doctor_id: int) -> dict:
    with dbm.identity_session(data_dir) as s:
        m = (s.query(im.Membership)
             .filter_by(user_id=actor, office_id=office_id, status="active")
             .first())
        if m is None:
            raise PermissionDenied("no active membership in this office")
    with dbm.office_session(data_dir, office_id) as osess:
        d = osess.get(om.Doctor, doctor_id)
        if d is None:
            raise NotFound("doctor not found")
        tgt = [dp.product_id for dp in osess.query(om.DoctorProduct)
               .filter_by(doctor_id=doctor_id).all()]
        last_survey = (osess.query(om.DoctorFieldSurvey)
                       .filter_by(doctor_id=doctor_id)
                       .order_by(om.DoctorFieldSurvey.id.desc()).first())
        history = (osess.query(om.DoctorHistory)
                   .filter_by(doctor_id=doctor_id)
                   .order_by(om.DoctorHistory.id.desc()).limit(50).all())
        return {
            "id": d.id, "external_code": d.external_code, "name": d.name,
            "area": d.area, "sub_area": d.sub_area,
            "nearest_pharmacy_id": d.nearest_pharmacy_id,
            "specialty": d.specialty, "doctor_class": d.doctor_class,
            "additional_info": d.additional_info,
            "reception_days": json.loads(d.reception_days_json or "[]"),
            "non_reception_days": json.loads(d.non_reception_days_json or "[]"),
            "reception_time": d.reception_time, "doctor_notes": d.doctor_notes,
            "dealer_info": d.dealer_info, "assigned_rep_id": d.assigned_rep_id,
            "status": d.status, "version": d.version,
            "tgt_products": tgt,
            "last_survey": ({
                "by": last_survey.rep_id,
                "at": last_survey.recorded_at.isoformat(),
                "market_notes": last_survey.market_notes,
            } if last_survey else None),
            "history": [{"field": h.field_name, "old": h.old_value,
                         "new": h.new_value, "by": h.changed_by,
                         "at": h.changed_at.isoformat()}
                        for h in history],
        }


def search_doctors(data_dir: Path, actor: int, *, office_id: int,
                   name: str = "", area: str = "", sub_area: str = "",
                   specialty: str = "", doctor_class: str = "",
                   status: str = "", assigned_rep_id: int | None = None,
                   product_id: int | None = None,
                   pharmacy_id: int | None = None,
                   limit: int = 100) -> list[dict]:
    with dbm.identity_session(data_dir) as s:
        m = (s.query(im.Membership)
             .filter_by(user_id=actor, office_id=office_id, status="active")
             .first())
        if m is None:
            raise PermissionDenied("no active membership in this office")
    with dbm.office_session(data_dir, office_id) as osess:
        q = osess.query(om.Doctor)
        if name:
            q = q.filter(om.Doctor.name.contains(name))
        if area:
            q = q.filter(om.Doctor.area == area)
        if sub_area:
            q = q.filter(om.Doctor.sub_area == sub_area)
        if specialty:
            q = q.filter(om.Doctor.specialty == specialty)
        if doctor_class:
            q = q.filter(om.Doctor.doctor_class == doctor_class)
        if status:
            q = q.filter(om.Doctor.status == status)
        if assigned_rep_id is not None:
            q = q.filter(om.Doctor.assigned_rep_id == assigned_rep_id)
        if pharmacy_id is not None:
            q = q.filter(om.Doctor.nearest_pharmacy_id == pharmacy_id)
        if product_id is not None:
            q = q.join(om.DoctorProduct,
                       om.DoctorProduct.doctor_id == om.Doctor.id
                       ).filter(om.DoctorProduct.product_id == product_id)
        return [{"id": d.id, "name": d.name, "area": d.area,
                 "sub_area": d.sub_area, "specialty": d.specialty,
                 "doctor_class": d.doctor_class, "status": d.status,
                 "assigned_rep_id": d.assigned_rep_id}
                for d in q.limit(limit).all()]


def _log_history(osess: Session, doctor_id: int, actor: int, field: str,
                 old: str, new: str, reason: str = "") -> None:
    osess.add(om.DoctorHistory(doctor_id=doctor_id, changed_by=actor,
                               field_name=field, old_value=old[:500],
                               new_value=new[:500], reason=reason))


def _require_rep(isess: Session, rep_user_id: int, office_id: int) -> None:
    m = (isess.query(im.Membership)
         .filter_by(user_id=rep_user_id, office_id=office_id, role="rep",
                    status="active").first())
    if m is None:
        raise ValidationError("assigned user is not an active rep here")


def _require_pharmacy(osess: Session, pharmacy_id: int) -> None:
    p = osess.get(om.Pharmacy, pharmacy_id)
    if p is None or p.status != "active":
        raise NotFound("pharmacy not found or inactive")
