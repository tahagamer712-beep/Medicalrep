"""Medical representative management.

Rules enforced here:
- add/edit/transfer/terminate all require rep.manage on the office.
- Reads resolve the office DB from the ACTOR's membership — a rep from
  office A can never read office B, even by passing B's record ids.
- Transfer keeps the old office's history untouched (new membership +
  new office row, old rows preserved with ended_at).
- Termination disables access; historical rows are never deleted.
"""
from __future__ import annotations

from pathlib import Path

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.identity import repositories as r
from app.services.audit import log_office_event


def _rep_row(osess, rep_user_id: int) -> om.Representative:
    row = osess.query(om.Representative).filter_by(user_id=rep_user_id).first()
    if row is None:
        raise NotFound("representative not found in this office")
    return row


def add_representative(data_dir: Path, actor_user_id: int, *, office_id: int,
                       full_name: str, phone: str, employee_code: str,
                       supervisor_user_id: int | None = None,
                       hire_date: str = "") -> dict:
    """Create user + rep membership + office job row.

    Validates everything first; if the office write fails after the identity
    write, the membership is compensated (ended) so no half-created rep exists.
    """
    if not employee_code.strip():
        raise ValidationError("employee_code is required")
    membership = None
    user = None
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor_user_id, "rep.manage", office_id=office_id)
        office = r.get_office(s, office_id)
        if r.get_user_by_phone(s, phone):
            raise AlreadyExists(f"phone '{phone}' already registered")
        if supervisor_user_id is not None:
            _supervisor_in_office(s, supervisor_user_id, office_id)
        user = r.add_user(s, full_name=full_name.strip(), phone=phone.strip())
        membership = r.add_membership(s, user_id=user.id, company_id=office.company_id,
                                      office_id=office.id, role="rep")
        user_id = user.id
        membership_id = membership.id
    try:
        with dbm.office_session(data_dir, office_id) as osess:
            if osess.query(om.Representative).filter_by(employee_code=employee_code.strip()).first():
                raise AlreadyExists(f"employee_code '{employee_code}' already used")
            osess.add(om.Representative(user_id=user_id,
                                        employee_code=employee_code.strip(),
                                        supervisor_user_id=supervisor_user_id,
                                        hire_date=hire_date))
            log_office_event(osess, actor_user_id=actor_user_id,
                             event_type="REPRESENTATIVE_ADDED",
                             entity="representative", entity_id=user_id)
    except Exception:
        # compensate the identity write so no half-created rep remains
        with dbm.identity_session(data_dir) as s:
            mship = s.get(im.Membership, membership_id)
            if mship is not None and mship.status == "active":
                r.end_membership(s, membership_id, utcnow())
        raise
    with dbm.identity_session(data_dir) as s:
        r.log_central_event(s, actor_user_id=actor_user_id,
                            event_type="REPRESENTATIVE_ADDED", result="ok",
                            office_id=office_id, entity="user", entity_id=user_id)
    return {"user_id": user_id, "office_id": office_id, "employee_code": employee_code.strip()}


def _supervisor_in_office(s, supervisor_user_id: int, office_id: int) -> None:
    """R2: a supervisor must hold an active membership in the same office."""
    from app.core.permissions import active_memberships
    ok = any(m.office_id == office_id and m.role in ("supervisor", "manager")
             for m in active_memberships(s, supervisor_user_id))
    if not ok:
        raise ValidationError("supervisor is not assigned to this office")


def update_representative(data_dir: Path, actor_user_id: int, *, office_id: int,
                          rep_user_id: int, full_name: str | None = None,
                          phone: str | None = None, notes: str | None = None,
                          supervisor_user_id: int | None = None) -> dict:
    # R1: verify the rep row exists in THIS office BEFORE touching identity —
    # otherwise a cross-office rep_user_id would get its identity data changed
    # even though the office write fails afterwards.
    with dbm.office_session(data_dir, office_id) as osess:
        _rep_row(osess, rep_user_id)
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor_user_id, "rep.manage", office_id=office_id)
        user = r.get_user(s, rep_user_id)
        if full_name is not None:
            user.full_name = full_name.strip()
        if phone is not None and phone.strip() != user.phone:
            if r.get_user_by_phone(s, phone.strip()):
                raise AlreadyExists(f"phone '{phone}' already registered")
            user.phone = phone.strip()
        if supervisor_user_id is not None:
            _supervisor_in_office(s, supervisor_user_id, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        row = _rep_row(osess, rep_user_id)
        if notes is not None:
            row.notes = notes
        if supervisor_user_id is not None:
            row.supervisor_user_id = supervisor_user_id
        row.updated_at = utcnow()
        log_office_event(osess, actor_user_id=actor_user_id,
                         event_type="REPRESENTATIVE_UPDATED",
                         entity="representative", entity_id=rep_user_id)
    return {"user_id": rep_user_id, "office_id": office_id}


def get_representative(data_dir: Path, actor_user_id: int, *, office_id: int,
                       rep_user_id: int) -> dict:
    """Permission-checked read. The office DB is chosen by the actor's grant,
    so cross-office id guessing yields NotFound, never leaked data."""
    with dbm.identity_session(data_dir) as s:
        try:
            require_permission(s, actor_user_id, "rep.self", office_id=office_id)
        except PermissionDenied:
            # also allow managers/supervisors via rep.manage for convenience
            require_permission(s, actor_user_id, "rep.manage", office_id=office_id)
        user = r.get_user(s, rep_user_id)
    with dbm.office_session(data_dir, office_id) as osess:
        row = _rep_row(osess, rep_user_id)
        return {
            "user_id": user.id, "full_name": user.full_name, "phone": user.phone,
            "employee_code": row.employee_code, "job_status": row.job_status,
            "hire_date": row.hire_date, "notes": row.notes,
        }


def transfer_representative(data_dir: Path, actor_user_id: int, *,
                            rep_user_id: int, from_office_id: int,
                            to_office_id: int, new_employee_code: str,
                            reason: str = "") -> dict:
    """Move a rep between offices. Old office history is preserved untouched.

    R4: the destination office row is inserted FIRST; identity changes come
    after. If identity changes fail, the destination row is compensated
    (deleted) so no orphan membership/row pair remains.
    """
    if from_office_id == to_office_id:
        raise ValidationError("source and destination offices are the same")
    if not new_employee_code.strip():
        raise ValidationError("new employee_code is required")
    with dbm.identity_session(data_dir) as s:
        r.get_office(s, from_office_id)
        dst = r.get_office(s, to_office_id)
        # transfer needs management rights on BOTH offices (or owner)
        try:
            require_permission(s, actor_user_id, "rep.manage", office_id=from_office_id)
            require_permission(s, actor_user_id, "rep.manage", office_id=to_office_id)
        except PermissionDenied:
            require_permission(s, actor_user_id, "company.create")  # owner fallback
        old = (s.query(im.Membership)
               .filter(im.Membership.user_id == rep_user_id,
                       im.Membership.office_id == from_office_id,
                       im.Membership.status == "active").first())
        if old is None:
            raise NotFound("active membership in source office not found")
        dst_company_id = dst.company_id
    # destination row first (compensatable)
    with dbm.office_session(data_dir, to_office_id) as osess:
        if osess.query(om.Representative).filter_by(employee_code=new_employee_code.strip()).first():
            raise AlreadyExists("employee_code already used in destination office")
        osess.add(om.Representative(user_id=rep_user_id,
                                    employee_code=new_employee_code.strip()))
    try:
        with dbm.identity_session(data_dir) as s:
            old = (s.query(im.Membership)
                   .filter(im.Membership.user_id == rep_user_id,
                           im.Membership.office_id == from_office_id,
                           im.Membership.status == "active").first())
            if old is None:
                raise NotFound("active membership in source office not found")
            r.end_membership(s, old.id, utcnow())
            r.add_membership(s, user_id=rep_user_id, company_id=dst_company_id,
                             office_id=to_office_id, role="rep")
            r.log_central_event(s, actor_user_id=actor_user_id,
                                event_type="REPRESENTATIVE_TRANSFERRED", result="ok",
                                office_id=to_office_id, entity="user",
                                entity_id=rep_user_id, reason=reason)
    except Exception:
        # compensate: remove the destination row we just created
        with dbm.office_session(data_dir, to_office_id) as osess:
            row = osess.query(om.Representative).filter_by(user_id=rep_user_id).first()
            if row is not None:
                osess.delete(row)
        raise
    with dbm.office_session(data_dir, to_office_id) as osess:
        log_office_event(osess, actor_user_id=actor_user_id,
                         event_type="REPRESENTATIVE_TRANSFERRED_IN",
                         entity="representative", entity_id=rep_user_id,
                         reason=reason)
    with dbm.office_session(data_dir, from_office_id) as osess:
        try:
            row = _rep_row(osess, rep_user_id)
            row.job_status = "ended"
            row.updated_at = utcnow()
        except NotFound:
            pass
        log_office_event(osess, actor_user_id=actor_user_id,
                         event_type="REPRESENTATIVE_TRANSFERRED_OUT",
                         entity="representative", entity_id=rep_user_id,
                         reason=reason)
    return {"user_id": rep_user_id, "from_office": from_office_id, "to_office": to_office_id}


def terminate_association(data_dir: Path, actor_user_id: int, *, rep_user_id: int,
                          office_id: int, reason: str = "") -> dict:
    """End the rep's association: access disabled, history preserved."""
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor_user_id, "rep.manage", office_id=office_id)
        mship = (s.query(im.Membership)
                 .filter(im.Membership.user_id == rep_user_id,
                         im.Membership.office_id == office_id,
                         im.Membership.status == "active").first())
        if mship is None:
            raise NotFound("active membership not found")
        r.end_membership(s, mship.id, utcnow())
        r.log_central_event(s, actor_user_id=actor_user_id,
                            event_type="REPRESENTATIVE_TERMINATED", result="ok",
                            office_id=office_id, entity="user", entity_id=rep_user_id,
                            reason=reason)
    with dbm.office_session(data_dir, office_id) as osess:
        try:
            row = _rep_row(osess, rep_user_id)
            row.job_status = "ended"
            row.updated_at = utcnow()
        except NotFound:
            pass
        log_office_event(osess, actor_user_id=actor_user_id,
                         event_type="REPRESENTATIVE_TERMINATED",
                         entity="representative", entity_id=rep_user_id,
                         reason=reason)
    return {"user_id": rep_user_id, "office_id": office_id, "status": "ended"}
