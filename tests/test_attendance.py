"""Attendance: server-time check-in/out, no duplicates, audited corrections."""
from datetime import datetime, timezone

import pytest

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.databases import manager as dbm
from app.databases import office_models as om
from app.services import attendance as att


def _at(h, m):
    return datetime(2026, 10, 11, h, m, tzinfo=timezone.utc)  # 08:00 UTC = 11:00 Baghdad


def test_check_in_out_flow(seed):
    d = seed["data_dir"]
    # 07:30 UTC = 10:30 Baghdad -> 2.5h late vs 08:00 work start
    ci = att.check_in(d, seed["rep1_o1"], office_id=seed["office1"],
                      rep_user_id=seed["rep1_o1"], now=_at(7, 30))
    assert ci["late_minutes"] == 150
    co = att.check_out(d, seed["rep1_o1"], office_id=seed["office1"],
                       rep_user_id=seed["rep1_o1"], now=_at(13, 0))
    assert co["record_id"] == ci["record_id"]
    rep = att.day_report(d, seed["supervisor1"], office_id=seed["office1"],
                         date="2026-10-11")
    assert len(rep) == 1 and rep[0]["status"] == "late"


def test_duplicate_check_in_rejected(seed):
    d = seed["data_dir"]
    att.check_in(d, seed["rep1_o1"], office_id=seed["office1"],
                 rep_user_id=seed["rep1_o1"], now=_at(5, 0))
    with pytest.raises(AlreadyExists):
        att.check_in(d, seed["rep1_o1"], office_id=seed["office1"],
                     rep_user_id=seed["rep1_o1"], now=_at(5, 30))


def test_duplicate_check_out_rejected(seed):
    d = seed["data_dir"]
    att.check_in(d, seed["rep1_o1"], office_id=seed["office1"],
                 rep_user_id=seed["rep1_o1"], now=_at(5, 0))
    att.check_out(d, seed["rep1_o1"], office_id=seed["office1"],
                  rep_user_id=seed["rep1_o1"], now=_at(13, 0))
    with pytest.raises(AlreadyExists):
        att.check_out(d, seed["rep1_o1"], office_id=seed["office1"],
                      rep_user_id=seed["rep1_o1"], now=_at(14, 0))


def test_check_out_without_check_in_rejected(seed):
    d = seed["data_dir"]
    with pytest.raises(ValidationError):
        att.check_out(d, seed["rep1_o1"], office_id=seed["office1"],
                      rep_user_id=seed["rep1_o1"], now=_at(13, 0))


def test_correct_attendance_logged(seed):
    d = seed["data_dir"]
    ci = att.check_in(d, seed["rep1_o1"], office_id=seed["office1"],
                      rep_user_id=seed["rep1_o1"], now=_at(5, 0))
    out = att.correct_attendance(
        d, seed["supervisor1"], office_id=seed["office1"],
        record_id=ci["record_id"], reason="نسي البصمة",
        check_in_at=datetime(2026, 10, 11, 4, 55, tzinfo=timezone.utc))
    assert out["status"] == "present"
    with dbm.office_session(d, seed["office1"]) as s:
        rec = s.get(om.AttendanceRecord, ci["record_id"])
        assert rec.corrected_by == seed["supervisor1"]
        assert rec.correction_reason == "نسي البصمة"
        evts = [e.event_type for e in s.query(om.OfficeAuditEvent).all()]
        assert "ATTENDANCE_CORRECTED" in evts


def test_rep_cannot_correct_attendance(seed):
    d = seed["data_dir"]
    ci = att.check_in(d, seed["rep1_o1"], office_id=seed["office1"],
                      rep_user_id=seed["rep1_o1"], now=_at(5, 0))
    with pytest.raises(PermissionDenied):
        att.correct_attendance(d, seed["rep1_o1"], office_id=seed["office1"],
                               record_id=ci["record_id"], reason="x")


def test_terminated_rep_cannot_check_in(seed):
    from app.services import representatives as reps
    d = seed["data_dir"]
    reps.terminate_association(d, seed["owner"], rep_user_id=seed["rep1_o1"],
                               office_id=seed["office1"], reason="test")
    with pytest.raises(PermissionDenied):
        att.check_in(d, seed["rep1_o1"], office_id=seed["office1"],
                     rep_user_id=seed["rep1_o1"], now=_at(5, 0))


def test_rep_sees_only_own_report(seed):
    d = seed["data_dir"]
    att.check_in(d, seed["rep1_o1"], office_id=seed["office1"],
                 rep_user_id=seed["rep1_o1"], now=_at(5, 0))
    att.check_in(d, seed["rep2_o1"], office_id=seed["office1"],
                 rep_user_id=seed["rep2_o1"], now=_at(5, 5))
    mine = att.day_report(d, seed["rep1_o1"], office_id=seed["office1"],
                          date="2026-10-11")
    assert [r["rep"] for r in mine] == [seed["rep1_o1"]]
    allr = att.day_report(d, seed["supervisor1"], office_id=seed["office1"],
                          date="2026-10-11")
    assert len(allr) == 2
