"""Meetings: scheduling, attendees, attendance marking.

meetings + holidays live here (both are calendar entities).
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.core.time_utils import ensure_utc, utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services.audit import log_office_event


def _active_member_ids(data_dir: Path, office_id: int, user_ids: list[int]) -> None:
    with dbm.identity_session(data_dir) as s:
        for uid in user_ids:
            m = (s.query(im.Membership)
                 .filter_by(user_id=uid, office_id=office_id, status="active")
                 .first())
            if m is None:
                raise ValidationError(f"user {uid} has no active membership here")


def schedule_meeting(data_dir: Path, actor: int, *, office_id: int, title: str,
                     scheduled_at: datetime, attendee_ids: list[int],
                     location: str = "", agenda: str = "",
                     meeting_type: str = "regular") -> dict:
    if not title.strip():
        raise ValidationError("meeting title is required")
    if not attendee_ids:
        raise ValidationError("at least one attendee is required")
    at = ensure_utc(scheduled_at)
    if at <= utcnow():
        raise ValidationError("meeting must be scheduled in the future")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "meeting.manage", office_id=office_id)
    _active_member_ids(data_dir, office_id, attendee_ids)
    with dbm.office_session(data_dir, office_id) as osess:
        mtg = om.Meeting(title=title.strip(), meeting_type=meeting_type,
                         scheduled_at=at, location=location, agenda=agenda,
                         created_by=actor)
        osess.add(mtg)
        osess.flush()
        for uid in set(attendee_ids):
            osess.add(om.MeetingAttendee(meeting_id=mtg.id, user_id=uid))
        log_office_event(osess, actor_user_id=actor, event_type="MEETING_SCHEDULED",
                         entity="meeting", entity_id=mtg.id)
        return {"meeting_id": mtg.id, "attendees": len(set(attendee_ids))}


def record_meeting_attendance(data_dir: Path, actor: int, *, office_id: int,
                              meeting_id: int, user_id: int,
                              status: str) -> dict:
    if status not in ("attended", "absent"):
        raise ValidationError("status must be attended or absent")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "meeting.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        mtg = osess.get(om.Meeting, meeting_id)
        if mtg is None:
            raise NotFound("meeting not found")
        if mtg.status == "cancelled":
            raise ValidationError("meeting was cancelled")
        att = (osess.query(om.MeetingAttendee)
               .filter_by(meeting_id=meeting_id, user_id=user_id).first())
        if att is None:
            raise NotFound("user is not an attendee of this meeting")
        att.status = status
        att.marked_at = utcnow()
        return {"meeting_id": meeting_id, "user_id": user_id, "status": status}


def set_meeting_status(data_dir: Path, actor: int, *, office_id: int,
                       meeting_id: int, status: str) -> dict:
    if status not in ("done", "cancelled"):
        raise ValidationError("status must be done or cancelled")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "meeting.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        mtg = osess.get(om.Meeting, meeting_id)
        if mtg is None:
            raise NotFound("meeting not found")
        if mtg.status != "scheduled":
            raise ValidationError(f"meeting is already {mtg.status}")
        mtg.status = status
        log_office_event(osess, actor_user_id=actor,
                         event_type="MEETING_" + status.upper(),
                         entity="meeting", entity_id=mtg.id)
        return {"meeting_id": mtg.id, "status": mtg.status}


def list_meetings(data_dir: Path, actor: int, *, office_id: int,
                  upcoming_only: bool = True) -> list[dict]:
    with dbm.identity_session(data_dir) as s:
        m = (s.query(im.Membership)
             .filter_by(user_id=actor, office_id=office_id, status="active").first())
        if m is None:
            raise PermissionDenied("no active membership in this office")
    with dbm.office_session(data_dir, office_id) as osess:
        q = osess.query(om.Meeting).order_by(om.Meeting.scheduled_at)
        if upcoming_only:
            q = q.filter(om.Meeting.status == "scheduled",
                         om.Meeting.scheduled_at >= utcnow())
        return [{"meeting_id": m.id, "title": m.title,
                 "scheduled_at": m.scheduled_at.isoformat(),
                 "status": m.status} for m in q.all()]


# --- holidays ---------------------------------------------------------------

def add_holiday(data_dir: Path, actor: int, *, office_id: int, date: str,
                name: str) -> dict:
    if not name.strip():
        raise ValidationError("holiday name is required")
    try:
        from datetime import date as d
        d.fromisoformat(date)
    except ValueError:
        raise ValidationError("date must be YYYY-MM-DD")
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "holiday.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        if osess.query(om.Holiday).filter_by(date=date).first():
            raise AlreadyExists("holiday already exists on this date")
        h = om.Holiday(date=date, name=name.strip(), created_by=actor)
        osess.add(h)
        osess.flush()
        return {"id": h.id, "date": h.date, "name": h.name}


def remove_holiday(data_dir: Path, actor: int, *, office_id: int,
                   holiday_id: int) -> None:
    with dbm.identity_session(data_dir) as s:
        require_permission(s, actor, "holiday.manage", office_id=office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        h = osess.get(om.Holiday, holiday_id)
        if h is None:
            raise NotFound("holiday not found")
        osess.delete(h)


def list_holidays(data_dir: Path, actor: int, *, office_id: int) -> list[dict]:
    with dbm.identity_session(data_dir) as s:
        m = (s.query(im.Membership)
             .filter_by(user_id=actor, office_id=office_id, status="active").first())
        if m is None:
            raise PermissionDenied("no active membership in this office")
    with dbm.office_session(data_dir, office_id) as osess:
        return [{"id": h.id, "date": h.date, "name": h.name}
                for h in osess.query(om.Holiday).order_by(om.Holiday.date).all()]


def is_holiday(data_dir: Path, office_id: int, date: str) -> bool:
    with dbm.office_session(data_dir, office_id) as osess:
        return osess.query(om.Holiday).filter_by(date=date).first() is not None
