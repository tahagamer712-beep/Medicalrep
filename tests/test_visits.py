"""Visits: planned vs actual strictly separated; corrections audited."""
import pytest

from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.databases import manager as dbm
from app.databases import office_models as om
from app.services import visits as vsvc


@pytest.fixture()
def doctor(seed):
    d = seed["data_dir"]
    return vsvc.add_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                           name="د. زياد", specialty="باطنية",
                           phone="07111111111")["id"]


def test_record_visit_requires_explicit_confirm(seed, doctor):
    d = seed["data_dir"]
    with pytest.raises(ValidationError):
        vsvc.record_actual_visit(
            d, seed["rep1_o1"], office_id=seed["office1"],
            rep_user_id=seed["rep1_o1"], target_type="doctor",
            target_id=doctor, confirm=False)


def test_record_and_list_visit(seed, doctor):
    d = seed["data_dir"]
    out = vsvc.record_actual_visit(
        d, seed["rep1_o1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], target_type="doctor", target_id=doctor,
        visit_type="routine", outcome="done", notes="زيارة جيدة",
        products=[{"name": "دواء أ", "qty": 2}], confirm=True)
    assert out["visit_id"] > 0
    visits = vsvc.list_actual_visits(d, seed["rep1_o1"],
                                     office_id=seed["office1"])
    assert len(visits) == 1
    assert visits[0]["outcome"] == "done"


def test_planned_visit_never_becomes_actual(seed, doctor):
    """A planned visit row cannot leak into actual visits."""
    from app.services import plans
    d = seed["data_dir"]
    p = plans.create_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                          plan_type="weekly", rep_user_id=seed["rep1_o1"],
                          period="2026-10-12", payload={})
    plans.add_planned_visits(
        d, seed["rep1_o1"], office_id=seed["office1"], plan_type="weekly",
        plan_id=p["plan_id"],
        visits=[{"target_type": "doctor", "target_id": doctor}])
    actual = vsvc.list_actual_visits(d, seed["owner"], office_id=seed["office1"],
                                     rep_user_id=seed["rep1_o1"])
    assert actual == []
    with dbm.office_session(d, seed["office1"]) as s:
        assert s.query(om.PlannedVisit).count() == 1
        assert s.query(om.ActualVisit).count() == 0


def test_correct_visit_creates_new_row(seed, doctor):
    d = seed["data_dir"]
    out = vsvc.record_actual_visit(
        d, seed["rep1_o1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], target_type="doctor", target_id=doctor,
        outcome="done", confirm=True)
    fixed = vsvc.correct_actual_visit(
        d, seed["supervisor1"], office_id=seed["office1"],
        visit_id=out["visit_id"], reason="نتيجة خاطئة", outcome="postponed")
    assert fixed["correction_of"] == out["visit_id"]
    with dbm.office_session(d, seed["office1"]) as s:
        old = s.get(om.ActualVisit, out["visit_id"])
        new = s.get(om.ActualVisit, fixed["visit_id"])
        assert old.status == "corrected"  # history preserved
        assert new.outcome == "postponed"
        assert new.correction_of == old.id
        evts = [e.event_type for e in s.query(om.OfficeAuditEvent).all()]
        assert "VISIT_CORRECTED" in evts


def test_correct_visit_requires_reason(seed, doctor):
    d = seed["data_dir"]
    out = vsvc.record_actual_visit(
        d, seed["rep1_o1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], target_type="doctor", target_id=doctor,
        confirm=True)
    with pytest.raises(ValidationError):
        vsvc.correct_actual_visit(d, seed["supervisor1"],
                                  office_id=seed["office1"],
                                  visit_id=out["visit_id"], reason="  ")


def test_rep_cannot_correct_own_visit(seed, doctor):
    d = seed["data_dir"]
    out = vsvc.record_actual_visit(
        d, seed["rep1_o1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], target_type="doctor", target_id=doctor,
        confirm=True)
    with pytest.raises(PermissionDenied):
        vsvc.correct_actual_visit(d, seed["rep1_o1"], office_id=seed["office1"],
                                  visit_id=out["visit_id"], reason="x")


def test_cross_office_visit_isolation(seed, doctor):
    d = seed["data_dir"]
    out = vsvc.record_actual_visit(
        d, seed["rep1_o1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], target_type="doctor", target_id=doctor,
        confirm=True)
    # supervisor of office 2 is authorized in office 2, but the visit id
    # does not exist there -> NotFound, never leaked data
    with pytest.raises(NotFound):
        vsvc.correct_actual_visit(d, seed["supervisor2"],
                                  office_id=seed["office2"],
                                  visit_id=out["visit_id"], reason="x")
    # and cannot even list it
    visits = vsvc.list_actual_visits(d, seed["supervisor2"],
                                     office_id=seed["office2"])
    assert visits == []
    # rep from office 1 has no scope in office 2 at all -> PermissionDenied
    with pytest.raises(PermissionDenied):
        vsvc.list_actual_visits(d, seed["rep1_o1"], office_id=seed["office2"])


def test_visit_to_unknown_target_rejected(seed):
    d = seed["data_dir"]
    with pytest.raises(NotFound):
        vsvc.record_actual_visit(
            d, seed["rep1_o1"], office_id=seed["office1"],
            rep_user_id=seed["rep1_o1"], target_type="doctor", target_id=99999,
            confirm=True)


def test_terminated_rep_cannot_record(seed, doctor):
    from app.services import representatives as reps
    d = seed["data_dir"]
    reps.terminate_association(d, seed["owner"], rep_user_id=seed["rep1_o1"],
                               office_id=seed["office1"], reason="test")
    with pytest.raises(PermissionDenied):
        vsvc.record_actual_visit(
            d, seed["rep1_o1"], office_id=seed["office1"],
            rep_user_id=seed["rep1_o1"], target_type="doctor",
            target_id=doctor, confirm=True)
