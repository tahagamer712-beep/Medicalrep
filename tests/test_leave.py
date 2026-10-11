"""Leave: types, requests, review, balances, isolation."""
import pytest

from app.core.errors import AlreadyExists, PermissionDenied, ValidationError
from app.services import leave as lv


@pytest.fixture()
def annual(seed):
    d = seed["data_dir"]
    return lv.create_leave_type(d, seed["supervisor1"], office_id=seed["office1"],
                                name="سنوية", default_days_per_year=30)["id"]


def test_request_and_approve(seed, annual):
    d = seed["data_dir"]
    r = lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                         rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                         start_date="2026-11-01", end_date="2026-11-03",
                         reason="سفر")
    assert r["status"] == "pending" and r["days"] == 3
    out = lv.review_leave(d, seed["supervisor1"], office_id=seed["office1"],
                          request_id=r["request_id"], decision="approve",
                          notes="موافق")
    assert out["status"] == "approved"
    bal = lv.leave_balance(d, seed["rep1_o1"], office_id=seed["office1"],
                            rep_user_id=seed["rep1_o1"], year=2026)
    assert bal[0]["remaining"] == 27


def test_reject_flow(seed, annual):
    d = seed["data_dir"]
    r = lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                         rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                         start_date="2026-11-01", end_date="2026-11-01")
    out = lv.review_leave(d, seed["supervisor1"], office_id=seed["office1"],
                          request_id=r["request_id"], decision="reject",
                          notes="ضغط عمل")
    assert out["status"] == "rejected"


def test_overlap_rejected(seed, annual):
    d = seed["data_dir"]
    lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                     rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                     start_date="2026-11-01", end_date="2026-11-05")
    with pytest.raises(AlreadyExists):
        lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                         rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                         start_date="2026-11-04", end_date="2026-11-06")


def test_exceeding_balance_rejected(seed, annual):
    d = seed["data_dir"]
    with pytest.raises(ValidationError):
        lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                         rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                         start_date="2026-11-01", end_date="2026-12-05")


def test_rep_cannot_approve(seed, annual):
    d = seed["data_dir"]
    r = lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                         rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                         start_date="2026-11-01", end_date="2026-11-01")
    with pytest.raises(PermissionDenied):
        lv.review_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                        request_id=r["request_id"], decision="approve")


def test_rep_cancels_own_pending(seed, annual):
    d = seed["data_dir"]
    r = lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                         rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                         start_date="2026-11-01", end_date="2026-11-01")
    out = lv.cancel_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                          request_id=r["request_id"])
    assert out["status"] == "cancelled"


def test_supervisor_cancels_approved(seed, annual):
    d = seed["data_dir"]
    r = lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                         rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                         start_date="2026-11-01", end_date="2026-11-01")
    lv.review_leave(d, seed["supervisor1"], office_id=seed["office1"],
                    request_id=r["request_id"], decision="approve")
    out = lv.cancel_leave(d, seed["supervisor1"], office_id=seed["office1"],
                          request_id=r["request_id"], reason="حاجة العمل")
    assert out["status"] == "cancelled"


def test_terminated_rep_cannot_request(seed, annual):
    from app.services import representatives as reps
    d = seed["data_dir"]
    reps.terminate_association(d, seed["owner"], rep_user_id=seed["rep1_o1"],
                               office_id=seed["office1"], reason="test")
    with pytest.raises(PermissionDenied):
        lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                         rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                         start_date="2026-11-01", end_date="2026-11-01")


def test_leave_isolated_per_office(seed, annual):
    d = seed["data_dir"]
    lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                     rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                     start_date="2026-11-01", end_date="2026-11-01")
    reqs = lv.list_leave_requests(d, seed["supervisor2"],
                                  office_id=seed["office2"])
    assert reqs == []
    with pytest.raises(PermissionDenied):
        lv.list_leave_requests(d, seed["rep1_o1"], office_id=seed["office2"])


def test_rep_sees_only_own_requests(seed, annual):
    d = seed["data_dir"]
    lv.request_leave(d, seed["rep1_o1"], office_id=seed["office1"],
                     rep_user_id=seed["rep1_o1"], leave_type_id=annual,
                     start_date="2026-11-01", end_date="2026-11-01")
    lv.request_leave(d, seed["rep2_o1"], office_id=seed["office1"],
                     rep_user_id=seed["rep2_o1"], leave_type_id=annual,
                     start_date="2026-11-02", end_date="2026-11-02")
    mine = lv.list_leave_requests(d, seed["rep1_o1"], office_id=seed["office1"])
    assert [r["rep"] for r in mine] == [seed["rep1_o1"]]
    allr = lv.list_leave_requests(d, seed["supervisor1"],
                                  office_id=seed["office1"])
    assert len(allr) == 2
