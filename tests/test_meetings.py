"""Meetings & holidays."""
from datetime import datetime, timedelta, timezone

import pytest

from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.services import meetings as mtg


def _future():
    return datetime.now(timezone.utc) + timedelta(days=2)


def test_schedule_and_attendance(seed):
    d = seed["data_dir"]
    m = mtg.schedule_meeting(
        d, seed["supervisor1"], office_id=seed["office1"],
        title="اجتماع المبيعات", scheduled_at=_future(),
        attendee_ids=[seed["rep1_o1"], seed["rep2_o1"]],
        location="المكتب", agenda="مراجعة الأهداف")
    assert m["attendees"] == 2
    mtg.record_meeting_attendance(d, seed["supervisor1"],
                                  office_id=seed["office1"],
                                  meeting_id=m["meeting_id"],
                                  user_id=seed["rep1_o1"], status="attended")
    mtg.set_meeting_status(d, seed["supervisor1"], office_id=seed["office1"],
                           meeting_id=m["meeting_id"], status="done")
    upcoming = mtg.list_meetings(d, seed["rep1_o1"], office_id=seed["office1"])
    assert upcoming == []  # done -> not upcoming


def test_rep_cannot_schedule(seed):
    d = seed["data_dir"]
    with pytest.raises(PermissionDenied):
        mtg.schedule_meeting(d, seed["rep1_o1"], office_id=seed["office1"],
                             title="x", scheduled_at=_future(),
                             attendee_ids=[seed["rep1_o1"]])


def test_past_meeting_rejected(seed):
    d = seed["data_dir"]
    with pytest.raises(ValidationError):
        mtg.schedule_meeting(
            d, seed["supervisor1"], office_id=seed["office1"], title="x",
            scheduled_at=datetime.now(timezone.utc) - timedelta(hours=1),
            attendee_ids=[seed["rep1_o1"]])


def test_attendee_must_be_office_member(seed):
    d = seed["data_dir"]
    with pytest.raises(ValidationError):
        mtg.schedule_meeting(d, seed["supervisor1"], office_id=seed["office1"],
                             title="x", scheduled_at=_future(),
                             attendee_ids=[seed["rep_o2"]])  # office 2 rep


def test_cancel_meeting(seed):
    d = seed["data_dir"]
    m = mtg.schedule_meeting(d, seed["supervisor1"], office_id=seed["office1"],
                             title="x", scheduled_at=_future(),
                             attendee_ids=[seed["rep1_o1"]])
    out = mtg.set_meeting_status(d, seed["supervisor1"],
                                 office_id=seed["office1"],
                                 meeting_id=m["meeting_id"], status="cancelled")
    assert out["status"] == "cancelled"
    with pytest.raises(ValidationError):
        mtg.record_meeting_attendance(d, seed["supervisor1"],
                                      office_id=seed["office1"],
                                      meeting_id=m["meeting_id"],
                                      user_id=seed["rep1_o1"],
                                      status="attended")


def test_holiday_manager_only(seed):
    d = seed["data_dir"]
    h = mtg.add_holiday(d, seed["manager1"], office_id=seed["office1"],
                        date="2026-12-25", name="عيد الميلاد")
    assert h["name"] == "عيد الميلاد"
    assert mtg.is_holiday(d, seed["office1"], "2026-12-25") is True
    assert mtg.is_holiday(d, seed["office1"], "2026-12-26") is False
    # supervisor cannot manage holidays (level 3 required)
    with pytest.raises(PermissionDenied):
        mtg.add_holiday(d, seed["supervisor1"], office_id=seed["office1"],
                        date="2026-12-26", name="x")
    mtg.remove_holiday(d, seed["manager1"], office_id=seed["office1"],
                       holiday_id=h["id"])
    assert mtg.is_holiday(d, seed["office1"], "2026-12-25") is False


def test_holiday_isolated_per_office(seed):
    d = seed["data_dir"]
    mtg.add_holiday(d, seed["manager1"], office_id=seed["office1"],
                    date="2026-12-25", name="عيد الميلاد")
    assert mtg.is_holiday(d, seed["office2"], "2026-12-25") is False
