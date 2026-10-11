"""Isolation tests: office A's users can never reach office B's data.

The critical property: services resolve the office database from the ACTOR's
membership, never from user-supplied ids. Guessing another office's record id
yields NotFound — never leaked rows.
"""
import pytest

from app.core.errors import NotFound, PermissionDenied
from app.databases import manager as dbm
from app.databases import office_models as om
from app.services import representatives as reps


def test_rep_cannot_read_other_office(seed):
    d = seed["data_dir"]
    # rep from office 1 tries to read a rep that lives in office 2
    with pytest.raises(PermissionDenied):
        reps.get_representative(d, seed["rep1_o1"],
                                office_id=seed["office2"],
                                rep_user_id=seed["rep_o2"])


def test_rep_cannot_reach_other_office_by_record_id(seed):
    d = seed["data_dir"]
    # rep1 (office 1) passes office-2 rep's user id but stays in office 1:
    # the lookup happens in office 1's OWN file -> NotFound, no leak.
    with pytest.raises(NotFound):
        reps.get_representative(d, seed["rep1_o1"],
                                office_id=seed["office1"],
                                rep_user_id=seed["rep_o2"])


def test_supervisor_company_a_cannot_write_company_b(seed):
    d = seed["data_dir"]
    with pytest.raises(PermissionDenied):
        reps.add_representative(
            d, seed["supervisor1"], office_id=seed["office2"],
            full_name="متسلل", phone="07000000999", employee_code="X-001")


def test_manager_scoped_to_own_company(seed):
    d = seed["data_dir"]
    # manager of company 1 CAN manage office 1 ...
    reps.get_representative(d, seed["manager1"], office_id=seed["office1"],
                            rep_user_id=seed["rep1_o1"])
    # ... but NOT office 2 (other company)
    with pytest.raises(PermissionDenied):
        reps.get_representative(d, seed["manager1"], office_id=seed["office2"],
                                rep_user_id=seed["rep_o2"])


def test_owner_can_read_both_offices(seed):
    d = seed["data_dir"]
    r1 = reps.get_representative(d, seed["owner"], office_id=seed["office1"],
                                 rep_user_id=seed["rep1_o1"])
    r2 = reps.get_representative(d, seed["owner"], office_id=seed["office2"],
                                 rep_user_id=seed["rep_o2"])
    assert r1["employee_code"] == "SHF-001"
    assert r2["employee_code"] == "RZI-001"


def test_office_files_are_physically_separate(seed):
    d = seed["data_dir"]
    p1 = dbm.office_db_path(d, seed["office1"])
    p2 = dbm.office_db_path(d, seed["office2"])
    assert p1 != p2 and p1.exists() and p2.exists()
    # rep2 (office 2) has NO row in office 1's file — check the raw file
    with dbm.office_session(d, seed["office1"]) as s:
        rows = s.query(om.Representative).all()
        assert {r.user_id for r in rows} == {seed["rep1_o1"], seed["rep2_o1"]}
