"""Plans: state machine, immutable revisions, permissions, isolation."""
import pytest

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.databases import manager as dbm
from app.databases import office_models as om
from app.services import plans


def _mk(seed, **kw):
    d = seed["data_dir"]
    kw.setdefault("office_id", seed["office1"])
    kw.setdefault("plan_type", "weekly")
    kw.setdefault("rep_user_id", seed["rep1_o1"])
    kw.setdefault("period", "2026-10-12")
    kw.setdefault("payload", {"sat": ["dr1"]})
    return plans.create_plan(d, seed["rep1_o1"], **kw)


def test_plan_lifecycle(seed):
    d = seed["data_dir"]
    p = _mk(seed)
    pid = p["plan_id"]
    assert p["status"] == "draft"

    # edit draft
    plans.update_plan_draft(d, seed["rep1_o1"], office_id=seed["office1"],
                            plan_type="weekly", plan_id=pid,
                            payload={"sat": ["dr1", "dr2"]})
    # submit -> revision snapshot
    r = plans.submit_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                          plan_type="weekly", plan_id=pid)
    assert r["status"] == "submitted" and r["version"] == 1

    # supervisor reviews
    plans.review_plan(d, seed["supervisor1"], office_id=seed["office1"],
                      plan_type="weekly", plan_id=pid, decision="start_review")
    plans.review_plan(d, seed["supervisor1"], office_id=seed["office1"],
                      plan_type="weekly", plan_id=pid, decision="approve",
                      notes="ممتاز")
    got = plans.get_plan(d, seed["owner"], office_id=seed["office1"],
                         plan_type="weekly", plan_id=pid)
    assert got["status"] == "approved"
    assert len(got["revisions"]) == 1
    assert got["payload"] == {"sat": ["dr1", "dr2"]}


def test_illegal_transitions_rejected(seed):
    d = seed["data_dir"]
    p = _mk(seed)
    # cannot approve a draft directly
    with pytest.raises(ValidationError):
        plans.review_plan(d, seed["supervisor1"], office_id=seed["office1"],
                          plan_type="weekly", plan_id=p["plan_id"],
                          decision="approve")
    # cannot edit after submit
    plans.submit_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                      plan_type="weekly", plan_id=p["plan_id"])
    with pytest.raises(ValidationError):
        plans.update_plan_draft(d, seed["rep1_o1"], office_id=seed["office1"],
                                plan_type="weekly", plan_id=p["plan_id"],
                                payload={})


def test_new_version_supersedes_old(seed):
    d = seed["data_dir"]
    p = _mk(seed)
    pid = p["plan_id"]
    plans.submit_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                      plan_type="weekly", plan_id=pid)
    plans.review_plan(d, seed["supervisor1"], office_id=seed["office1"],
                      plan_type="weekly", plan_id=pid, decision="start_review")
    plans.review_plan(d, seed["supervisor1"], office_id=seed["office1"],
                      plan_type="weekly", plan_id=pid, decision="approve")
    # rep starts a new version
    plans.update_plan_draft(d, seed["rep1_o1"], office_id=seed["office1"],
                            plan_type="weekly", plan_id=pid,
                            payload={"sat": ["dr9"]})
    r = plans.submit_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                          plan_type="weekly", plan_id=pid)
    assert r["version"] == 2
    got = plans.get_plan(d, seed["owner"], office_id=seed["office1"],
                         plan_type="weekly", plan_id=pid)
    assert len(got["revisions"]) == 2
    assert got["revisions"][0]["superseded_by"] == 2
    assert got["revisions"][1]["superseded_by"] is None


def test_changes_requested_cycle(seed):
    d = seed["data_dir"]
    p = _mk(seed)
    pid = p["plan_id"]
    plans.submit_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                      plan_type="weekly", plan_id=pid)
    plans.review_plan(d, seed["supervisor1"], office_id=seed["office1"],
                      plan_type="weekly", plan_id=pid, decision="start_review")
    plans.review_plan(d, seed["supervisor1"], office_id=seed["office1"],
                      plan_type="weekly", plan_id=pid,
                      decision="request_changes", notes="أضف زيارتين")
    plans.update_plan_draft(d, seed["rep1_o1"], office_id=seed["office1"],
                            plan_type="weekly", plan_id=pid, payload={"x": 1})
    got = plans.get_plan(d, seed["owner"], office_id=seed["office1"],
                         plan_type="weekly", plan_id=pid)
    assert got["status"] == "draft"  # back to draft after revision


def test_duplicate_period_rejected(seed):
    _mk(seed)
    with pytest.raises(AlreadyExists):
        _mk(seed)


def test_rep_cannot_touch_other_rep_plan(seed):
    d = seed["data_dir"]
    p = _mk(seed)  # rep1's plan
    with pytest.raises(PermissionDenied):
        plans.update_plan_draft(d, seed["rep2_o1"], office_id=seed["office1"],
                                plan_type="weekly", plan_id=p["plan_id"],
                                payload={})
    with pytest.raises(PermissionDenied):
        plans.submit_plan(d, seed["rep2_o1"], office_id=seed["office1"],
                          plan_type="weekly", plan_id=p["plan_id"])


def test_cross_office_plan_ids_isolated(seed):
    d = seed["data_dir"]
    p = _mk(seed)  # office 1 plan
    # supervisor of office 2 guesses the plan id in office 2 -> NotFound
    with pytest.raises(NotFound):
        plans.get_plan(d, seed["supervisor2"], office_id=seed["office2"],
                       plan_type="weekly", plan_id=p["plan_id"])
    # rep from office 1 cannot read office 2 scope at all
    with pytest.raises(PermissionDenied):
        plans.get_plan(d, seed["rep1_o1"], office_id=seed["office2"],
                       plan_type="weekly", plan_id=p["plan_id"])


def test_terminated_rep_cannot_plan(seed):
    from app.services import representatives as reps
    d = seed["data_dir"]
    reps.terminate_association(d, seed["owner"], rep_user_id=seed["rep1_o1"],
                               office_id=seed["office1"], reason="test")
    with pytest.raises(PermissionDenied):
        _mk(seed)


def test_planned_visits_attached_to_draft(seed):
    from app.services import visits as vsvc
    d = seed["data_dir"]
    doc = vsvc.add_doctor(d, seed["supervisor1"], office_id=seed["office1"],
                          name="د. اختبار")
    p = _mk(seed)
    n = plans.add_planned_visits(
        d, seed["rep1_o1"], office_id=seed["office1"], plan_type="weekly",
        plan_id=p["plan_id"],
        visits=[{"target_type": "doctor", "target_id": doc["id"],
                 "expected_time": "09:00"}])
    assert n == 1
    got = plans.get_plan(d, seed["owner"], office_id=seed["office1"],
                         plan_type="weekly", plan_id=p["plan_id"])
    assert got["planned_visits"][0]["target_id"] == doc["id"]


def test_daily_plan_flow(seed):
    d = seed["data_dir"]
    p = plans.create_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                          plan_type="daily", rep_user_id=seed["rep1_o1"],
                          period="2026-10-11", payload={"visits": 5})
    plans.submit_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                      plan_type="daily", plan_id=p["plan_id"])
    plans.review_plan(d, seed["supervisor1"], office_id=seed["office1"],
                      plan_type="daily", plan_id=p["plan_id"],
                      decision="start_review")
    plans.review_plan(d, seed["supervisor1"], office_id=seed["office1"],
                      plan_type="daily", plan_id=p["plan_id"], decision="approve")
    got = plans.get_plan(d, seed["owner"], office_id=seed["office1"],
                         plan_type="daily", plan_id=p["plan_id"])
    assert got["status"] == "approved"
