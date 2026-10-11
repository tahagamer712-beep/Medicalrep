"""End-to-end integration flows across services (dummy data only).

Each test exercises a full business flow through multiple services,
proving the pieces are actually wired together — not just unit-tested
in isolation.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.databases import manager as dbm
from app.identity import models as im
from app.identity import services as ids
from app.jobs import reminders
from app.jobs.notifier import FakeNotifier
from app.services import (attendance as att, doctors as docs,
                           leave as lv, payroll as pr, plans as pl,
                           products as prod, reports as rep,
                           visits as vs)


def _setup_company(d, owner):
    with dbm.identity_session(d) as s:
        c = ids.create_company(s, owner, name_ar="شركة التكامل",
                               name_en="integration co")
        o = ids.create_office(s, d, owner, company_id=c.id,
                              name_ar="مكتب التكامل", name_en="integration")
        mgr = ids.create_user(s, owner, full_name="مدير", phone="07900000011")
        ids.assign_membership(s, owner, user_id=mgr.id, company_id=c.id,
                              office_id=o.id, role="manager")
    from app.services import representatives as repsvc
    r = repsvc.add_representative(d, mgr.id, office_id=o.id,
                                  full_name="مندوب", phone="07900000012",
                                  employee_code="E2E-001")
    return c.id, o.id, mgr.id, r["user_id"]


def test_full_rep_day_flow(seed):
    """manager sets up -> rep checks in -> visit with products ->
    plan submitted+approved -> payroll deduction proposed+approved ->
    report shows it -> backup set restores it."""
    d = seed["data_dir"]
    owner = seed["owner"]
    cid, oid, mgr, rep1 = _setup_company(d, owner)

    # product in catalog
    unit = prod.create_unit(d, mgr, office_id=oid, name="علبة")
    p = prod.create_product(d, mgr, office_id=oid, trade_name="دواء أ",
                            unit_id=unit["id"])

    # doctor
    dr = docs.create_doctor(d, mgr, office_id=oid, name="د. تكامل",
                         specialty="باطنية")

    # attendance
    att.check_in(d, rep1, office_id=oid, rep_user_id=rep1)

    # visit with catalog products
    v = vs.record_actual_visit(
        d, rep1, office_id=oid, rep_user_id=rep1, target_type="doctor",
        target_id=dr["id"], products=[p["id"]], confirm=True)
    vs.record_visit_products(d, rep1, office_id=oid, visit_id=v["visit_id"],
                             items=[{"product_id": p["id"], "quantity": 2}])
    linked = vs.list_visit_products(d, rep1, office_id=oid, visit_id=v["visit_id"])
    assert len(linked) == 1 and linked[0]["product_id"] == p["id"]

    att.check_out(d, rep1, office_id=oid, rep_user_id=rep1)

    # weekly plan lifecycle
    plan = pl.create_plan(d, rep1, office_id=oid, plan_type="weekly",
                          rep_user_id=rep1, period="2026-10-12", payload={})
    pl.submit_plan(d, rep1, office_id=oid, plan_type="weekly",
                   plan_id=plan["plan_id"])
    pl.review_plan(d, mgr, office_id=oid, plan_type="weekly",
                   plan_id=plan["plan_id"], decision="start_review")
    pl.review_plan(d, mgr, office_id=oid, plan_type="weekly",
                   plan_id=plan["plan_id"], decision="approve")
    got = pl.get_plan(d, mgr, office_id=oid, plan_type="weekly",
                      plan_id=plan["plan_id"])
    assert got["status"] == "approved"

    # payroll: propose + approve deduction, then summary nets it
    period = pr.open_payroll_period(d, mgr, office_id=oid,
                                    year=2026, month=10)
    pr.set_base_salary(d, mgr, office_id=oid, period_id=period["period_id"], rep_user_id=rep1, amount=1000)
    prop = pr.propose_deduction(d, mgr, office_id=oid, rep_user_id=rep1,
                                period_id=period["period_id"], amount=100,
                                reason="تأخير")
    pr.review_proposed_deduction(d, mgr, office_id=oid,
                                 txn_uuid=prop["uuid"], decision="approve")
    summary = pr.salary_summary(d, mgr, office_id=oid, rep_user_id=rep1,
                                period_id=period["period_id"])
    assert summary["net"] == 900

    # report reflects the day
    r = rep.daily_report(d, mgr, office_id=oid, day="2026-10-11")
    assert r["totals"]["visits"] >= 1

    # backup set captures everything; restore brings it back
    ifile = dbm.backup_identity(d)
    ofile = dbm.backup_office(d, oid)
    manifest = dbm.write_backup_manifest(d, ifile, {oid: ofile})
    dbm.verify_backup_set(d, manifest)
    res = dbm.restore_backup_set(d, manifest, confirm=True)
    assert res["failed"] == []
    r2 = rep.daily_report(d, mgr, office_id=oid, day="2026-10-11")
    assert r2["totals"]["visits"] == r["totals"]["visits"]


def test_leave_approval_and_reminder_flow(seed):
    """leave request -> approval -> reminder job runs exactly once."""
    d = seed["data_dir"]
    owner = seed["owner"]
    cid, oid, mgr, rep1 = _setup_company(d, owner)

    lt = lv.create_leave_type(d, mgr, office_id=oid, name="سنوية",
                              default_days_per_year=21)
    req = lv.request_leave(d, rep1, office_id=oid, rep_user_id=rep1,
                           leave_type_id=lt["id"], start_date="2026-11-01",
                           end_date="2026-11-02", reason="راحة")
    lv.review_leave(d, mgr, office_id=oid, request_id=req["request_id"],
                    decision="approve")
    bal = lv.leave_balance(d, rep1, office_id=oid, rep_user_id=rep1, year=2026)
    row = next(r for r in bal if r["leave_type"] == "سنوية")
    assert row["remaining"] == 19

    # reminder machinery: ensure + run with fake notifier, twice = once sent
    from app.databases import office_models as om
    import json
    with dbm.office_session(d, oid) as s:
        s.add(om.NotificationJob(
            job_key="e2e:leave:1", kind="leave",
            scheduled_for=datetime(2020, 1, 1, tzinfo=timezone.utc),
            payload_json=json.dumps({"user_id": rep1,
                                     "text": "تمت الموافقة على إجازتك"})))
    n = FakeNotifier()
    reminders.run_due_jobs(d, None, n)
    reminders.run_due_jobs(d, None, n)
    assert len(n.inbox.get(rep1, [])) == 1


def test_office_isolation_e2e(seed):
    """Two offices: data created in one is invisible in the other."""
    d = seed["data_dir"]
    owner = seed["owner"]
    with dbm.identity_session(d) as s:
        c = ids.create_company(s, owner, name_ar="ش", name_en="c")
        o1 = ids.create_office(s, d, owner, company_id=c.id,
                               name_ar="م1", name_en="o1")
        o2 = ids.create_office(s, d, owner, company_id=c.id,
                               name_ar="م2", name_en="o2")
        m1 = ids.create_user(s, owner, full_name="م1", phone="07900000013")
        ids.assign_membership(s, owner, user_id=m1.id, company_id=c.id,
                              office_id=o1.id, role="manager")
    unit = prod.create_unit(d, m1.id, office_id=o1.id, name="علبة")
    p1 = prod.create_product(d, m1.id, office_id=o1.id, trade_name="دواء",
                             unit_id=unit["id"])
    got = prod.list_products(d, m1.id, office_id=o1.id)
    assert any(x["id"] == p1["id"] for x in got)
    # manager of office 1 cannot list office 2's catalog via office-1 perms
    with pytest.raises(Exception):
        prod.list_products(d, m1.id, office_id=o2.id)
