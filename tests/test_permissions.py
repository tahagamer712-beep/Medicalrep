"""Permission-model tests: roles, disabled users, ended memberships."""
import pytest

from app.core.errors import NotFound, PermissionDenied
from app.core.permissions import require_owner, require_permission
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.identity import models as im
from app.identity import repositories as r
from app.services import representatives as reps


def _idsess(seed):
    return dbm.identity_session(seed["data_dir"])


def test_unknown_user_denied(seed):
    with _idsess(seed) as s:
        with pytest.raises(PermissionDenied):
            require_permission(s, 999999, "rep.self", office_id=seed["office1"])


def test_unknown_action_denied(seed):
    with _idsess(seed) as s:
        with pytest.raises(PermissionDenied):
            require_permission(s, seed["owner"], "no.such.action",
                               office_id=seed["office1"])


def test_non_owner_cannot_create_company(seed):
    from app.identity import services as ids
    with _idsess(seed) as s:
        with pytest.raises(PermissionDenied):
            ids.create_company(s, seed["manager1"], name_ar="x", name_en="x")


def test_disabled_user_denied(seed):
    with _idsess(seed) as s:
        u = r.get_user(s, seed["rep1_o1"])
        u.status = "disabled"
    with _idsess(seed) as s:
        with pytest.raises(PermissionDenied):
            require_permission(s, seed["rep1_o1"], "rep.self",
                               office_id=seed["office1"])


def test_ended_membership_denied(seed):
    d = seed["data_dir"]
    reps.terminate_association(d, seed["owner"], rep_user_id=seed["rep1_o1"],
                               office_id=seed["office1"], reason="test")
    with _idsess(seed) as s:
        with pytest.raises(PermissionDenied):
            require_permission(s, seed["rep1_o1"], "rep.self",
                               office_id=seed["office1"])


def test_require_owner_rejects_non_owner(seed):
    with _idsess(seed) as s:
        with pytest.raises(PermissionDenied):
            require_owner(s, seed["manager1"])
        m = require_owner(s, seed["owner"])
        assert m.role == "owner"


def test_rep_self_read_own_record_ok(seed):
    d = seed["data_dir"]
    rec = reps.get_representative(d, seed["rep1_o1"], office_id=seed["office1"],
                                  rep_user_id=seed["rep1_o1"])
    assert rec["full_name"] == "مندوب أحمد"


def test_missing_office_raises_not_found(seed):
    # non-owner: office is resolved before membership matching
    with _idsess(seed) as s:
        with pytest.raises(NotFound):
            require_permission(s, seed["manager1"], "rep.self", office_id=424242)
