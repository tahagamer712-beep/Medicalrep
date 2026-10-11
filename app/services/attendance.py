"""Attendance: check-in / check-out with server time.

- One open record per rep per day: duplicate check-ins are rejected.
- check_out requires an open check-in from the same day.
- Corrections need attendance.correct + a reason; corrector and reason logged.
- Terminated reps (no active membership) cannot record anything new.
- late_minutes computed against the office work_start in office timezone.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.core.time_utils import ensure_utc, utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.identity import repositories as r
from app.services.audit import log_office_event
from app.services.visits import _require_active_rep, _require_self_or_manager


def _today_str(office_tz: str, now: datetime) -> str:
    return now.astimezone(ZoneInfo(office_tz)).strftime("%Y-%m-%d")


def _late_minutes(work_start: str, check_in: datetime, office_tz: str) -> int:
    try:
        hh, mm = (int(x) for x in work_start.split(":"))
    except ValueError:
        return 0
    local = check_in.astimezone(ZoneInfo(office_tz))
    start = local.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return max(0, int((local - start).total_seconds() // 60))


def check_in(data_dir: Path, actor: int, *, office_id: int, rep_user_id: int,
             now: datetime | None = None) -> dict:
    at = ensure_utc(now) if now else utcnow()
    with dbm.identity_session(data_dir) as s:
        _require_self_or_manager(s, actor, office_id, rep_user_id,
                                 "attendance.record", "attendance.correct")
        _require_active_rep(s, rep_user_id, office_id)
        office = r.get_office(s, office_id)
        tz, work_start = office.timezone, office.work_start
    day = _today_str(tz, at)
    with dbm.office_session(data_dir, office_id) as osess:
        existing = (osess.query(om.AttendanceRecord)
                    .filter_by(representative_id=rep_user_id, date=day).first())
        if existing is not None and existing.check_in_at is not None:
            raise AlreadyExists("already checked in today")
        rec = existing or om.AttendanceRecord(representative_id=rep_user_id,
                                              date=day)
        if existing is None:
            osess.add(rec)
        rec.check_in_at = at
        rec.late_minutes = _late_minutes(work_start, at, tz)
        rec.status = "late" if rec.late_minutes > 0 else "present"
        rec.updated_at = utcnow()
        osess.flush()
        log_office_event(osess, actor_user_id=actor, event_type="CHECK_IN",
                         entity="attendance", entity_id=rec.id)
        return {"record_id": rec.id, "date": day, "check_in": at.isoformat(),
                "late_minutes": rec.late_minutes}


def check_out(data_dir: Path, actor: int, *, office_id: int, rep_user_id: int,
              now: datetime | None = None) -> dict:
    at = ensure_utc(now) if now else utcnow()
    with dbm.identity_session(data_dir) as s:
        _require_self_or_manager(s, actor, office_id, rep_user_id,
                                 "attendance.record", "attendance.correct")
        _require_active_rep(s, rep_user_id, office_id)
        tz = r.get_office(s, office_id).timezone
    day = _today_str(tz, at)
    with dbm.office_session(data_dir, office_id) as osess:
        rec = (osess.query(om.AttendanceRecord)
               .filter_by(representative_id=rep_user_id, date=day).first())
        if rec is None or rec.check_in_at is None:
            raise ValidationError("no check-in found for today")
        if rec.check_out_at is not None:
            raise AlreadyExists("already checked out today")
        rec.check_out_at = at
        rec.updated_at = utcnow()
        log_office_event(osess, actor_user_id=actor, event_type="CHECK_OUT",
                         entity="attendance", entity_id=rec.id)
        return {"record_id": rec.id, "check_out": at.isoformat()}


def correct_attendance(data_dir: Path, actor: int, *, office_id: int,
                       record_id: int, reason: str, **changes) -> dict:
    """Supervisor correction. Reason + corrector recorded; audit logged."""
    if not reason.strip():
        raise ValidationError("correction reason is required")
    with dbm.identity_session(data_dir) as s:
        from app.core.permissions import require_permission
        require_permission(s, actor, "attendance.correct", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        rec = osess.get(om.AttendanceRecord, record_id)
        if rec is None:
            raise NotFound("attendance record not found")
        if "check_in_at" in changes:
            rec.check_in_at = ensure_utc(changes["check_in_at"])
        if "check_out_at" in changes:
            rec.check_out_at = ensure_utc(changes["check_out_at"])
        if "status" in changes:
            rec.status = changes["status"]
        rec.corrected_by = actor
        rec.correction_reason = reason.strip()
        rec.updated_at = utcnow()
        log_office_event(osess, actor_user_id=actor,
                         event_type="ATTENDANCE_CORRECTED",
                         entity="attendance", entity_id=rec.id, reason=reason)
        return {"record_id": rec.id, "status": rec.status}


def day_report(data_dir: Path, actor: int, *, office_id: int,
               date: str) -> list[dict]:
    """Who checked in/out on a given date. Supervisor+ or self."""
    with dbm.identity_session(data_dir) as s:
        from app.core.permissions import require_permission
        try:
            require_permission(s, actor, "attendance.correct", office_id=office_id)
            rep_filter = None
        except PermissionDenied:
            require_permission(s, actor, "attendance.record", office_id=office_id)
            rep_filter = actor
    with dbm.office_session(data_dir, office_id) as osess:
        q = osess.query(om.AttendanceRecord).filter_by(date=date)
        if rep_filter is not None:
            q = q.filter_by(representative_id=rep_filter)
        return [{"rep": rec.representative_id,
                 "check_in": rec.check_in_at.isoformat() if rec.check_in_at else None,
                 "check_out": rec.check_out_at.isoformat() if rec.check_out_at else None,
                 "status": rec.status, "late_minutes": rec.late_minutes}
                for rec in q.all()]
