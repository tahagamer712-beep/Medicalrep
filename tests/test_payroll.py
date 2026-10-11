"""Payroll: periods, direct vs proposed deductions, bonuses, reversals,
idempotency, isolation."""
import pytest

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.databases import manager as dbm
from app.databases import office_models as om
from app.jobs import reminders
from app.jobs.notifier import FakeNotifier
from app.services import payroll as pr


@pytest.fixture()
def period(seed):
    d = seed["data_dir"]
    p = pr.open_payroll_period(d, seed["manager1"], office_id=seed["office1"],
                               year=2026, month=10)
    pr.set_base_salary(d, seed["manager1"], office_id=seed["office1"],
                       period_id=p["period_id"], rep_user_id=seed["rep1_o1"],
                       amount=1_000_000)
    return p["period_id"]


def _summary(seed, period, rep=None):
    d = seed["data_dir"]
    return pr.salary_summary(d, seed["supervisor1"], office_id=seed["office1"],
                            rep_user_id=rep or seed["rep1_o1"],
                            period_id=period)


def test_direct_deduction_applies_immediately(seed, period):
    d = seed["data_dir"]
    out = pr.apply_direct_deduction(
        d, seed["supervisor1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], period_id=period, amount=50_000,
        reason="تأخير متكرر", idempotency_key="key-1")
    assert out["status"] == "applied"
    s = _summary(seed, period)
    assert s["deductions"] == 50_000 and s["net"] == 950_000
    # idempotent retry with the same key: no second movement
    out2 = pr.apply_direct_deduction(
        d, seed["supervisor1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], period_id=period, amount=50_000,
        reason="تأخير متكرر", idempotency_key="key-1")
    assert out2["uuid"] == out["uuid"]
    assert _summary(seed, period)["deductions"] == 50_000


def test_proposed_deduction_has_no_effect_until_approved(seed, period):
    d = seed["data_dir"]
    out = pr.propose_deduction(
        d, seed["supervisor1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], period_id=period, amount=30_000,
        reason="اقتراح آلي", idempotency_key="prop-1")
    assert out["status"] == "pending"
    assert _summary(seed, period)["deductions"] == 0  # no effect yet

    pr.review_proposed_deduction(d, seed["supervisor1"],
                                 office_id=seed["office1"],
                                 txn_uuid=out["uuid"], decision="approve")
    assert _summary(seed, period)["deductions"] == 30_000


def test_rejected_proposal_never_applies(seed, period):
    d = seed["data_dir"]
    out = pr.propose_deduction(
        d, seed["supervisor1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], period_id=period, amount=30_000,
        reason="اقتراح آلي")
    pr.review_proposed_deduction(d, seed["supervisor1"],
                                 office_id=seed["office1"],
                                 txn_uuid=out["uuid"], decision="reject")
    assert _summary(seed, period)["deductions"] == 0
    with dbm.office_session(d, seed["office1"]) as s:
        t = s.query(om.FinancialTransaction).filter_by(uuid=out["uuid"]).one()
        assert t.status == "rejected"  # row preserved, not deleted


def test_rep_cannot_deduct_or_propose(seed, period):
    d = seed["data_dir"]
    with pytest.raises(PermissionDenied):
        pr.apply_direct_deduction(
            d, seed["rep1_o1"], office_id=seed["office1"],
            rep_user_id=seed["rep2_o1"], period_id=period, amount=10_000,
            reason="x")
    with pytest.raises(PermissionDenied):
        pr.propose_deduction(d, seed["rep1_o1"], office_id=seed["office1"],
                             rep_user_id=seed["rep2_o1"], period_id=period,
                             amount=10_000, reason="x")


def test_rep_cannot_review_proposal(seed, period):
    d = seed["data_dir"]
    out = pr.propose_deduction(d, seed["supervisor1"], office_id=seed["office1"],
                               rep_user_id=seed["rep1_o1"], period_id=period,
                               amount=10_000, reason="x")
    with pytest.raises(PermissionDenied):
        pr.review_proposed_deduction(d, seed["rep1_o1"],
                                     office_id=seed["office1"],
                                     txn_uuid=out["uuid"], decision="approve")


def test_bonus_and_reversal(seed, period):
    d = seed["data_dir"]
    b = pr.grant_bonus(d, seed["supervisor1"], office_id=seed["office1"],
                       rep_user_id=seed["rep1_o1"], period_id=period,
                       amount=100_000, reason="أفضل مندوب")
    assert _summary(seed, period)["bonuses"] == 100_000
    rev = pr.reverse_transaction(d, seed["supervisor1"],
                                 office_id=seed["office1"],
                                 txn_uuid=b["uuid"], reason="خطأ إدخال")
    assert rev["reverses"] == b["uuid"]
    assert _summary(seed, period)["bonuses"] == 0
    # original row untouched (append-only)
    with dbm.office_session(d, seed["office1"]) as s:
        orig = s.query(om.FinancialTransaction).filter_by(uuid=b["uuid"]).one()
        assert orig.status == "applied" and orig.signed_amount == 100_000
        assert s.query(om.FinancialTransaction).count() == 2


def test_reversal_needs_reason(seed, period):
    d = seed["data_dir"]
    b = pr.grant_bonus(d, seed["supervisor1"], office_id=seed["office1"],
                       rep_user_id=seed["rep1_o1"], period_id=period,
                       amount=100_000, reason="x")
    with pytest.raises(ValidationError):
        pr.reverse_transaction(d, seed["supervisor1"], office_id=seed["office1"],
                               txn_uuid=b["uuid"], reason="  ")


def test_finalized_period_rejects_movements(seed, period):
    d = seed["data_dir"]
    pr.apply_direct_deduction(d, seed["supervisor1"], office_id=seed["office1"],
                              rep_user_id=seed["rep1_o1"], period_id=period,
                              amount=50_000, reason="x")
    out = pr.finalize_period(d, seed["manager1"], office_id=seed["office1"],
                             period_id=period)
    assert out["status"] == "finalized"
    s = _summary(seed, period)
    assert s["net"] == 950_000 and s["status"] == "finalized"
    with pytest.raises(ValidationError):
        pr.apply_direct_deduction(d, seed["supervisor1"],
                                  office_id=seed["office1"],
                                  rep_user_id=seed["rep1_o1"], period_id=period,
                                  amount=10_000, reason="late")


def test_duplicate_period_rejected(seed, period):
    d = seed["data_dir"]
    with pytest.raises(AlreadyExists):
        pr.open_payroll_period(d, seed["manager1"], office_id=seed["office1"],
                               year=2026, month=10)


def test_rep_views_own_summary_only(seed, period):
    d = seed["data_dir"]
    mine = pr.salary_summary(d, seed["rep1_o1"], office_id=seed["office1"],
                             rep_user_id=seed["rep1_o1"], period_id=period)
    assert mine["base_salary"] == 1_000_000
    with pytest.raises(PermissionDenied):
        pr.salary_summary(d, seed["rep1_o1"], office_id=seed["office1"],
                          rep_user_id=seed["rep2_o1"], period_id=period)
    txns = pr.list_transactions(d, seed["rep1_o1"], office_id=seed["office1"])
    assert all(t["rep"] == seed["rep1_o1"] for t in txns)


def test_payroll_isolated_per_office(seed, period):
    d = seed["data_dir"]
    pr.apply_direct_deduction(d, seed["supervisor1"], office_id=seed["office1"],
                              rep_user_id=seed["rep1_o1"], period_id=period,
                              amount=50_000, reason="x")
    txns = pr.list_transactions(d, seed["supervisor2"],
                                office_id=seed["office2"])
    assert txns == []
    with pytest.raises(PermissionDenied):
        pr.open_payroll_period(d, seed["manager2"], office_id=seed["office1"],
                               year=2026, month=11)


def test_direct_deduction_notifies_rep(seed, period):
    d = seed["data_dir"]
    pr.apply_direct_deduction(d, seed["supervisor1"], office_id=seed["office1"],
                              rep_user_id=seed["rep1_o1"], period_id=period,
                              amount=25_000, reason="تأخير")
    notifier = FakeNotifier()
    stats = reminders.run_due_jobs(d, None, notifier)
    assert stats["sent"] >= 1
    inbox = notifier.inbox.get(seed["rep1_o1"], [])
    assert any("خصم" in m for m in inbox)


def test_terminated_rep_no_new_movements(seed, period):
    from app.services import representatives as reps
    d = seed["data_dir"]
    reps.terminate_association(d, seed["owner"], rep_user_id=seed["rep1_o1"],
                               office_id=seed["office1"], reason="test")
    with pytest.raises(PermissionDenied):
        pr.apply_direct_deduction(d, seed["supervisor1"],
                                  office_id=seed["office1"],
                                  rep_user_id=seed["rep1_o1"], period_id=period,
                                  amount=10_000, reason="x")


def test_reversed_deduction_nets_to_zero(seed, period):
    d = seed["data_dir"]
    out = pr.apply_direct_deduction(
        d, seed["supervisor1"], office_id=seed["office1"],
        rep_user_id=seed["rep1_o1"], period_id=period, amount=40_000,
        reason="x")
    assert _summary(seed, period)["deductions"] == 40_000
    pr.reverse_transaction(d, seed["supervisor1"], office_id=seed["office1"],
                           txn_uuid=out["uuid"], reason="عذر مقبول")
    s = _summary(seed, period)
    assert s["deductions"] == 0 and s["net"] == 1_000_000


def test_base_salary_history_with_effective_dates(seed):
    d = seed["data_dir"]
    # raise effective 2026-11-01, before opening November period
    pr.change_base_salary(d, seed["manager1"], office_id=seed["office1"],
                          rep_user_id=seed["rep1_o1"], amount=1_200_000,
                          effective_from="2026-11-01", reason="زيادة سنوية")
    p = pr.open_payroll_period(d, seed["manager1"], office_id=seed["office1"],
                               year=2026, month=11)
    s = pr.salary_summary(d, seed["supervisor1"], office_id=seed["office1"],
                          rep_user_id=seed["rep1_o1"], period_id=p["period_id"])
    assert s["base_salary"] == 1_200_000  # raise picked up automatically
    hist = pr.list_salary_history(d, seed["rep1_o1"], office_id=seed["office1"],
                                  rep_user_id=seed["rep1_o1"])
    assert hist[-1]["amount"] == 1_200_000
    assert hist[-1]["effective_from"] == "2026-11-01"


def test_concurrent_same_idempotency_key(seed, period):
    """Two simultaneous submits with the same key -> exactly one movement,
    one audit entry, one notification."""
    import threading

    d = seed["data_dir"]
    errs = []
    results = []

    def submit():
        try:
            results.append(pr.apply_direct_deduction(
                d, seed["supervisor1"], office_id=seed["office1"],
                rep_user_id=seed["rep1_o1"], period_id=period, amount=10_000,
                reason="concurrent", idempotency_key="conc-1"))
        except Exception as e:  # noqa: BLE001
            errs.append(e)

    ts = [threading.Thread(target=submit) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert not errs
    assert len({r["uuid"] for r in results}) == 1
    assert sum(1 for r in results if r["created"]) == 1
    with dbm.office_session(d, seed["office1"]) as s:
        assert s.query(om.FinancialTransaction).filter_by(
            uuid="conc-1").count() == 1
        # no duplicate notification on retry
        assert s.query(om.NotificationJob).filter_by(
            kind="deduction_notice").count() == 1
