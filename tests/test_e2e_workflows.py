"""End-to-end workflows: rep action -> supervisor -> manager -> owner,
with company/office isolation verified at every step.

These are the integration proof that services work TOGETHER, not just alone.
"""
from datetime import datetime, timezone

import pytest

from app.core.errors import NotFound, PermissionDenied
from app.jobs import reminders
from app.jobs.notifier import FakeNotifier
from app.services import attendance as att
from app.services import plans, representatives as reps
from app.services import visits as vsvc

WEEK = "2026-10-12"


def _submit_plan(seed, rep, office):
    d = seed["data_dir"]
    p = plans.create_plan(d, rep, office_id=office, plan_type="weekly",
                          rep_user_id=rep, period=WEEK,
                          payload={"sat": ["clinic"]})
    plans.submit_plan(d, rep, office_id=office, plan_type="weekly",
                      plan_id=p["plan_id"])
    return p["plan_id"]


def test_plan_workflow_across_roles(seed):
    d = seed["data_dir"]
    o1, o2 = seed["office1"], seed["office2"]
    pid = _submit_plan(seed, seed["rep1_o1"], o1)

    # supervisor of the same office reviews and approves
    plans.review_plan(d, seed["supervisor1"], office_id=o1, plan_type="weekly",
                      plan_id=pid, decision="start_review")
    plans.review_plan(d, seed["supervisor1"], office_id=o1, plan_type="weekly",
                      plan_id=pid, decision="approve")
    got = plans.get_plan(d, seed["supervisor1"], office_id=o1,
                         plan_type="weekly", plan_id=pid)
    assert got["status"] == "approved"

    # manager of the same company sees it (review rights via company)
    got = plans.get_plan(d, seed["manager1"], office_id=o1,
                         plan_type="weekly", plan_id=pid)
    assert got["status"] == "approved"

    # manager of the OTHER company: denied
    with pytest.raises(PermissionDenied):
        plans.get_plan(d, seed["manager2"], office_id=o1,
                       plan_type="weekly", plan_id=pid)

    # supervisor of the other office: not found in their office, never leaked
    with pytest.raises(NotFound):
        plans.get_plan(d, seed["supervisor2"], office_id=o2,
                       plan_type="weekly", plan_id=pid)

    # owner sees everything
    got = plans.get_plan(d, seed["owner"], office_id=o1,
                         plan_type="weekly", plan_id=pid)
    assert got["revisions"][0]["version"] == 1


def test_visit_and_attendance_visibility(seed):
    d = seed["data_dir"]
    o1, o2 = seed["office1"], seed["office2"]
    doc = vsvc.add_doctor(d, seed["supervisor1"], office_id=o1, name="د. س")
    vsvc.record_actual_visit(
        d, seed["rep1_o1"], office_id=o1, rep_user_id=seed["rep1_o1"],
        target_type="doctor", target_id=doc["id"], confirm=True)
    att.check_in(d, seed["rep1_o1"], office_id=o1, rep_user_id=seed["rep1_o1"],
                 now=datetime(2026, 10, 11, 5, 0, tzinfo=timezone.utc))

    # supervisor sees rep's visit and attendance
    visits = vsvc.list_actual_visits(d, seed["supervisor1"], office_id=o1)
    assert len(visits) == 1
    day = att.day_report(d, seed["supervisor1"], office_id=o1, date="2026-10-11")
    assert len(day) == 1

    # other office sees nothing
    assert vsvc.list_actual_visits(d, seed["supervisor2"], office_id=o2) == []
    assert att.day_report(d, seed["supervisor2"], office_id=o2,
                          date="2026-10-11") == []

    # owner sees across offices
    assert len(vsvc.list_actual_visits(d, seed["owner"], office_id=o1)) == 1


def test_termination_stops_future_preserves_history(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    pid = _submit_plan(seed, seed["rep1_o1"], o1)
    doc = vsvc.add_doctor(d, seed["supervisor1"], office_id=o1, name="د. ق")
    vsvc.record_actual_visit(
        d, seed["rep1_o1"], office_id=o1, rep_user_id=seed["rep1_o1"],
        target_type="doctor", target_id=doc["id"], confirm=True)

    reps.terminate_association(d, seed["owner"], rep_user_id=seed["rep1_o1"],
                               office_id=o1, reason="e2e")

    # no new actions
    with pytest.raises(PermissionDenied):
        plans.create_plan(d, seed["rep1_o1"], office_id=o1, plan_type="weekly",
                          rep_user_id=seed["rep1_o1"], period="2026-10-19",
                          payload={})
    with pytest.raises(PermissionDenied):
        vsvc.record_actual_visit(
            d, seed["rep1_o1"], office_id=o1, rep_user_id=seed["rep1_o1"],
            target_type="doctor", target_id=doc["id"], confirm=True)

    # history still readable by supervisor and owner
    got = plans.get_plan(d, seed["supervisor1"], office_id=o1,
                         plan_type="weekly", plan_id=pid)
    assert got["status"] == "submitted"
    visits = vsvc.list_actual_visits(d, seed["owner"], office_id=o1,
                                     rep_user_id=seed["rep1_o1"])
    assert len(visits) == 1


def test_transfer_keeps_per_office_history(seed):
    d = seed["data_dir"]
    o1, o2 = seed["office1"], seed["office2"]
    pid = _submit_plan(seed, seed["rep1_o1"], o1)

    reps.transfer_representative(
        d, seed["owner"], rep_user_id=seed["rep1_o1"],
        from_office_id=o1, to_office_id=o2, new_employee_code="RZI-010",
        reason="e2e")

    # old office history intact and visible to old supervisor
    got = plans.get_plan(d, seed["supervisor1"], office_id=o1,
                         plan_type="weekly", plan_id=pid)
    assert got["status"] == "submitted"

    # rep works in the new office now
    p2 = plans.create_plan(d, seed["rep1_o1"], office_id=o2, plan_type="weekly",
                           rep_user_id=seed["rep1_o1"], period="2026-10-19",
                           payload={})
    assert p2["status"] == "draft"

    # but cannot act in the old office anymore
    with pytest.raises(PermissionDenied):
        plans.create_plan(d, seed["rep1_o1"], office_id=o1, plan_type="weekly",
                          rep_user_id=seed["rep1_o1"], period="2026-10-26",
                          payload={})

    # new office supervisor sees the new plan; ids are per-file, so the same
    # numeric id in office 2 resolves to office 2's plan — never office 1's
    got2 = plans.get_plan(d, seed["supervisor2"], office_id=o2,
                          plan_type="weekly", plan_id=p2["plan_id"])
    assert got2["status"] == "draft"
    assert got2["period"] == "2026-10-19"  # office 2's plan, not office 1's


def test_reminders_cancelled_after_termination(seed):
    d = seed["data_dir"]
    o1 = seed["office1"]
    now = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)
    reminders.ensure_weekly_reminder_jobs(d, o1, now)

    reps.terminate_association(d, seed["owner"], rep_user_id=seed["rep1_o1"],
                               office_id=o1, reason="e2e")

    later = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
    notifier = FakeNotifier()
    stats = reminders.run_due_jobs(d, later, notifier)
    # rep2's job sent; rep1's job cancelled (not sent, not failed)
    assert stats["sent"] == 1
    assert stats.get("cancelled", 0) == 1
    assert seed["rep1_o1"] not in notifier.inbox
    assert seed["rep2_o1"] in notifier.inbox
