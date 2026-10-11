"""Leave management: types, requests, review, balances.

- Reps request their own leave (supervisor+ may request for a team member).
- No overlapping pending/approved requests for the same rep.
- Requests beyond the yearly balance are rejected (configurable assumption).
- Review: approve | reject with notes. Cancel: rep cancels own pending;
  supervisor+ may cancel pending or approved.
"""
from __future__ import annotations

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


def _office_access(isess: Session, actor: int, office_id: int) -> bool:
    try:
        require_permission(isess, actor, "leave.review", office_id=office_id)
        return True
    except PermissionDenied:
        require_permission(isess, actor, "leave.request", office_id=office_id)
        return False


def _require_active_rep(isess: Session, rep_user_id: int, office_id: int) -> None:
    m = (isess.query(im.Membership)
         .filter_by(user_id=rep_user_id, office_id=office_id, role="rep",
                    status="active").first())
    if m is None:
        raise PermissionDenied("representative has no active membership here")


def _parse_day(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError("date must be YYYY-MM-DD")


def create_leave_type(data_dir: Path, actor: int, *, office_id: int, name: str,
                      default_days_per_year: int = 0,
                      requires_attachment: bool = False) -> dict:
    if not name.strip():
        raise ValidationError("leave type name is required")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "leave.review", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        if osess.query(om.LeaveType).filter_by(name=name.strip()).first():
            raise AlreadyExists("leave type already exists")
        t = om.LeaveType(name=name.strip(),
                         default_days_per_year=int(default_days_per_year),
                         requires_attachment=bool(requires_attachment))
        osess.add(t)
        osess.flush()
        return {"id": t.id, "name": t.name}


def request_leave(data_dir: Path, actor: int, *, office_id: int,
                  rep_user_id: int, leave_type_id: int, start_date: str,
                  end_date: str, reason: str = "",
                  attachment_path: str = "") -> dict:
    start, end = _parse_day(start_date), _parse_day(end_date)
    if end < start:
        raise ValidationError("end_date is before start_date")
    days = (end - start).days + 1
    with dbm.identity_session(data_dir) as s:
        is_reviewer = _office_access(s, actor, office_id)
        if not is_reviewer and actor != rep_user_id:
            raise PermissionDenied("can only request own leave")
        _require_active_rep(s, rep_user_id, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        ltype = osess.get(om.LeaveType, leave_type_id)
        if ltype is None or ltype.status != "active":
            raise NotFound("leave type not found or inactive")
        if ltype.requires_attachment and not attachment_path:
            raise ValidationError("this leave type requires an attachment")
        # no overlap with pending/approved
        clash = (osess.query(om.LeaveRequest)
                 .filter(om.LeaveRequest.representative_id == rep_user_id,
                         om.LeaveRequest.status.in_(("pending", "approved")),
                         om.LeaveRequest.start_date <= end.isoformat(),
                         om.LeaveRequest.end_date >= start.isoformat())
                 .first())
        if clash:
            raise AlreadyExists("overlaps an existing leave request")
        # balance check
        bal = _balance(osess, rep_user_id, leave_type_id, start.year)
        if days > bal["remaining"]:
            raise ValidationError(
                f"only {bal['remaining']} days remaining for {ltype.name}")
        req = om.LeaveRequest(
            representative_id=rep_user_id, leave_type_id=leave_type_id,
            start_date=start.isoformat(), end_date=end.isoformat(),
            days_count=days, reason=reason, attachment_path=attachment_path)
        osess.add(req)
        osess.flush()
        log_office_event(osess, actor_user_id=actor, event_type="LEAVE_REQUESTED",
                         entity="leave_request", entity_id=req.id)
        return {"request_id": req.id, "status": req.status, "days": days}


def review_leave(data_dir: Path, actor: int, *, office_id: int, request_id: int,
                 decision: str, notes: str = "") -> dict:
    if decision not in ("approve", "reject"):
        raise ValidationError("decision must be approve or reject")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "leave.review", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        req = osess.get(om.LeaveRequest, request_id)
        if req is None:
            raise NotFound("leave request not found")
        allowed = om.LEAVE_TRANSITIONS.get(req.status, set())
        target = "approved" if decision == "approve" else "rejected"
        if target not in allowed:
            raise ValidationError(f"cannot {decision} a {req.status} request")
        req.status = target
        req.reviewed_by = actor
        req.reviewed_at = utcnow()
        req.review_notes = notes
        req.updated_at = utcnow()
        log_office_event(osess, actor_user_id=actor, event_type="LEAVE_REVIEWED",
                         entity="leave_request", entity_id=req.id, reason=notes)
        return {"request_id": req.id, "status": req.status}


def cancel_leave(data_dir: Path, actor: int, *, office_id: int,
                 request_id: int, reason: str = "") -> dict:
    with dbm.identity_session(data_dir) as s:
        is_reviewer = _office_access(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        req = osess.get(om.LeaveRequest, request_id)
        if req is None:
            raise NotFound("leave request not found")
        if not is_reviewer:
            if req.representative_id != actor:
                raise PermissionDenied("can only cancel own leave")
            if req.status != "pending":
                raise ValidationError("can only cancel a pending request")
        if "cancelled" not in om.LEAVE_TRANSITIONS.get(req.status, set()):
            raise ValidationError(f"cannot cancel a {req.status} request")
        req.status = "cancelled"
        req.updated_at = utcnow()
        log_office_event(osess, actor_user_id=actor, event_type="LEAVE_CANCELLED",
                         entity="leave_request", entity_id=req.id, reason=reason)
        return {"request_id": req.id, "status": req.status}


def leave_balance(data_dir: Path, actor: int, *, office_id: int,
                  rep_user_id: int, year: int) -> list[dict]:
    with dbm.identity_session(data_dir) as s:
        is_reviewer = _office_access(s, actor, office_id)
        if not is_reviewer and actor != rep_user_id:
            raise PermissionDenied("can only view own balance")
    with dbm.office_session(data_dir, office_id) as osess:
        out = []
        for t in osess.query(om.LeaveType).filter_by(status="active").all():
            out.append({"leave_type": t.name, **_balance(osess, rep_user_id, t.id, year)})
        return out


def list_leave_requests(data_dir: Path, actor: int, *, office_id: int,
                        status: str | None = None) -> list[dict]:
    with dbm.identity_session(data_dir) as s:
        is_reviewer = _office_access(s, actor, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        q = osess.query(om.LeaveRequest).order_by(om.LeaveRequest.id.desc())
        if not is_reviewer:
            q = q.filter_by(representative_id=actor)
        if status:
            q = q.filter_by(status=status)
        return [{"request_id": r.id, "rep": r.representative_id,
                 "period": f"{r.start_date}..{r.end_date}", "days": r.days_count,
                 "status": r.status} for r in q.limit(100).all()]


def _balance(osess: Session, rep_user_id: int, leave_type_id: int,
             year: int) -> dict:
    t = osess.get(om.LeaveType, leave_type_id)
    used = sum(
        r.days_count for r in osess.query(om.LeaveRequest).filter_by(
            representative_id=rep_user_id, leave_type_id=leave_type_id,
            status="approved").all()
        if r.start_date.startswith(str(year)))
    entitlement = t.default_days_per_year if t else 0
    return {"entitlement": entitlement, "used": used,
            "remaining": max(0, entitlement - used)}
