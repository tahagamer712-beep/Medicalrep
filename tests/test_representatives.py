"""Representative lifecycle: add / edit / transfer / terminate.

History is never deleted: transfer keeps the old office's rows, termination
disables access while preserving every historical record.
"""
import pytest

from app.core.errors import AlreadyExists, NotFound, PermissionDenied, ValidationError
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services import representatives as reps


def _office_rep_ids(data_dir, office_id):
    with dbm.office_session(data_dir, office_id) as s:
        return {r.user_id for r in s.query(om.Representative).all()}


def test_add_representative(seed):
    d = seed["data_dir"]
    out = reps.add_representative(
        d, seed["supervisor1"], office_id=seed["office1"],
        full_name="مندوب جديد", phone="07000000021", employee_code="SHF-003")
    assert out["employee_code"] == "SHF-003"
    assert out["user_id"] in _office_rep_ids(d, seed["office1"])
    rec = reps.get_representative(d, seed["owner"], office_id=seed["office1"],
                                  rep_user_id=out["user_id"])
    assert rec["full_name"] == "مندوب جديد"


def test_add_rep_duplicate_phone_rejected(seed):
    d = seed["data_dir"]
    with pytest.raises(AlreadyExists):
        reps.add_representative(
            d, seed["supervisor1"], office_id=seed["office1"],
            full_name="مكرر", phone="07000000011", employee_code="SHF-009")


def test_add_rep_duplicate_code_compensates_membership(seed):
    """Office write fails on duplicate code -> identity membership is ended,
    so no half-created rep remains usable."""
    d = seed["data_dir"]
    with pytest.raises(AlreadyExists):
        reps.add_representative(
            d, seed["supervisor1"], office_id=seed["office1"],
            full_name="كود مكرر", phone="07000000022", employee_code="SHF-001")
    with dbm.identity_session(d) as s:
        u = s.query(im.User).filter_by(phone="07000000022").one()
        mships = (s.query(im.Membership)
                  .filter_by(user_id=u.id, office_id=seed["office1"]).all())
        assert mships and all(m.status == "ended" for m in mships)


def test_update_representative(seed):
    d = seed["data_dir"]
    reps.update_representative(d, seed["supervisor1"], office_id=seed["office1"],
                               rep_user_id=seed["rep1_o1"],
                               notes="ملاحظة محدثة")
    rec = reps.get_representative(d, seed["owner"], office_id=seed["office1"],
                                  rep_user_id=seed["rep1_o1"])
    assert rec["notes"] == "ملاحظة محدثة"


def test_update_rep_duplicate_phone_rejected(seed):
    d = seed["data_dir"]
    with pytest.raises(AlreadyExists):
        reps.update_representative(d, seed["supervisor1"], office_id=seed["office1"],
                                   rep_user_id=seed["rep1_o1"],
                                   phone="07000000012")  # rep2's phone


def test_transfer_preserves_old_office_history(seed):
    d = seed["data_dir"]
    out = reps.transfer_representative(
        d, seed["owner"], rep_user_id=seed["rep1_o1"],
        from_office_id=seed["office1"], to_office_id=seed["office2"],
        new_employee_code="RZI-002", reason="نقل تجريبي")
    assert out["to_office"] == seed["office2"]

    # old office history untouched: row still there, marked ended
    with dbm.office_session(d, seed["office1"]) as s:
        row = s.query(om.Representative).filter_by(user_id=seed["rep1_o1"]).one()
        assert row.job_status == "ended"
        assert row.employee_code == "SHF-001"  # original code preserved

    # new office has the new row
    rec = reps.get_representative(d, seed["owner"], office_id=seed["office2"],
                                  rep_user_id=seed["rep1_o1"])
    assert rec["employee_code"] == "RZI-002"

    # old membership ended, new one active
    with dbm.identity_session(d) as s:
        old = (s.query(im.Membership)
               .filter_by(user_id=seed["rep1_o1"], office_id=seed["office1"]).one())
        new = (s.query(im.Membership)
               .filter_by(user_id=seed["rep1_o1"], office_id=seed["office2"],
                          status="active").one())
        assert old.status == "ended" and old.ended_at is not None
        assert new.role == "rep"

    # rep can no longer act in the old office, but can in the new one
    with pytest.raises(PermissionDenied):
        reps.get_representative(d, seed["rep1_o1"], office_id=seed["office1"],
                                rep_user_id=seed["rep2_o1"])
    rec2 = reps.get_representative(d, seed["rep1_o1"], office_id=seed["office2"],
                                   rep_user_id=seed["rep1_o1"])
    assert rec2["employee_code"] == "RZI-002"


def test_transfer_same_office_rejected(seed):
    d = seed["data_dir"]
    with pytest.raises(ValidationError):
        reps.transfer_representative(
            d, seed["owner"], rep_user_id=seed["rep1_o1"],
            from_office_id=seed["office1"], to_office_id=seed["office1"],
            new_employee_code="X")


def test_terminate_disables_access_preserves_history(seed):
    d = seed["data_dir"]
    reps.terminate_association(d, seed["supervisor1"], rep_user_id=seed["rep2_o1"],
                               office_id=seed["office1"], reason="استقالة تجريبية")
    # access disabled
    with pytest.raises(PermissionDenied):
        reps.get_representative(d, seed["rep2_o1"], office_id=seed["office1"],
                                rep_user_id=seed["rep2_o1"])
    # history preserved: row still in office DB, marked ended
    with dbm.office_session(d, seed["office1"]) as s:
        row = s.query(om.Representative).filter_by(user_id=seed["rep2_o1"]).one()
        assert row.job_status == "ended"
    # owner can still read the historical record
    rec = reps.get_representative(d, seed["owner"], office_id=seed["office1"],
                                  rep_user_id=seed["rep2_o1"])
    assert rec["employee_code"] == "SHF-002"


def test_audit_trail_records_rep_lifecycle(seed):
    d = seed["data_dir"]
    with dbm.office_session(d, seed["office1"]) as s:
        types = {e.event_type for e in s.query(om.OfficeAuditEvent).all()}
    assert "REPRESENTATIVE_ADDED" in types
    with dbm.identity_session(d) as s:
        central = {e.event_type for e in s.query(im.CentralAuditEvent).all()}
    assert "REPRESENTATIVE_ADDED" in central
    assert "OWNER_CREATED_COMPANY" in central


def test_update_cross_office_rep_does_not_touch_identity(seed):
    """R1: passing another office's rep id must fail BEFORE any identity write."""
    d = seed["data_dir"]
    with dbm.identity_session(d) as s:
        name_before = s.query(im.User).filter_by(id=seed["rep_o2"]).one().full_name
    with pytest.raises(NotFound):
        reps.update_representative(d, seed["supervisor1"], office_id=seed["office1"],
                                   rep_user_id=seed["rep_o2"],
                                   full_name="مخترق")
    with dbm.identity_session(d) as s:
        name_after = s.query(im.User).filter_by(id=seed["rep_o2"]).one().full_name
    assert name_after == name_before == "مندوب علي"


def test_add_rep_with_foreign_supervisor_rejected(seed):
    """R2: supervisor must belong to the same office."""
    d = seed["data_dir"]
    with pytest.raises(ValidationError):
        reps.add_representative(
            d, seed["supervisor1"], office_id=seed["office1"],
            full_name="x", phone="07000000031", employee_code="SHF-031",
            supervisor_user_id=seed["supervisor2"])  # office 2's supervisor


def test_transfer_compensates_destination_row_on_identity_failure(seed, monkeypatch):
    """R4: if identity changes fail after the destination row insert,
    the destination row is removed — no orphan."""
    d = seed["data_dir"]
    from app.identity import repositories as r

    real_add = r.add_membership

    def boom(*a, **k):
        raise RuntimeError("simulated identity failure")

    monkeypatch.setattr(r, "add_membership", boom)
    with pytest.raises(RuntimeError):
        reps.transfer_representative(
            d, seed["owner"], rep_user_id=seed["rep1_o1"],
            from_office_id=seed["office1"], to_office_id=seed["office2"],
            new_employee_code="RZI-090")
    # destination row compensated
    with dbm.office_session(d, seed["office2"]) as s:
        assert s.query(om.Representative).filter_by(user_id=seed["rep1_o1"]).first() is None
    # old membership still active (untouched)
    with dbm.identity_session(d) as s:
        old = (s.query(im.Membership)
               .filter_by(user_id=seed["rep1_o1"], office_id=seed["office1"],
                          status="active").one_or_none())
        assert old is not None
    monkeypatch.setattr(r, "add_membership", real_add)
