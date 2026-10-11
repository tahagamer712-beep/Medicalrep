"""Backup & restore: encrypted per-office backups, verified restores."""
import pytest

from app.databases import manager as dbm
from app.databases import office_models as om
from app.services import representatives as reps


def _rep_count(data_dir, office_id):
    with dbm.office_session(data_dir, office_id) as s:
        return s.query(om.Representative).count()


def test_backup_creates_encrypted_file(seed, tmp_path):
    d = seed["data_dir"]
    out = dbm.backup_office(d, seed["office1"])
    assert out.exists() and out.suffix == ".enc"
    assert out.stat().st_size > 0
    # key lives separately from the backup, with 0600 perms
    key_file = d / "office_keys" / f"office_{seed['office1']}.key"
    assert key_file.exists()
    assert oct(key_file.stat().st_mode & 0o777) == "0o600"
    # backup bytes are NOT a readable sqlite file
    assert not out.read_bytes().startswith(b"SQLite format 3")


def test_restore_recovers_data(seed):
    d = seed["data_dir"]
    before = _rep_count(d, seed["office1"])
    backup = dbm.backup_office(d, seed["office1"])
    # change live data afterwards
    reps.terminate_association(d, seed["owner"], rep_user_id=seed["rep1_o1"],
                               office_id=seed["office1"], reason="tmp")
    assert _rep_count(d, seed["office1"]) == before  # row preserved, status changed
    dbm.restore_office(d, seed["office1"], backup, confirm=True)
    assert _rep_count(d, seed["office1"]) == before
    # terminated rep is active again after restore (snapshot semantics)
    with dbm.office_session(d, seed["office1"]) as s:
        row = s.query(om.Representative).filter_by(user_id=seed["rep1_o1"]).one()
        assert row.job_status == "active"


def test_restore_requires_confirm(seed):
    d = seed["data_dir"]
    backup = dbm.backup_office(d, seed["office1"])
    before = _rep_count(d, seed["office1"])
    with pytest.raises(ValueError):
        dbm.restore_office(d, seed["office1"], backup, confirm=False)
    assert _rep_count(d, seed["office1"]) == before  # untouched


def test_restore_corrupt_backup_fails_safe(seed):
    d = seed["data_dir"]
    backup = dbm.backup_office(d, seed["office1"])
    before = _rep_count(d, seed["office1"])
    raw = bytearray(backup.read_bytes())
    raw[20] ^= 0xFF  # tamper
    backup.write_bytes(bytes(raw))
    with pytest.raises(Exception):
        dbm.restore_office(d, seed["office1"], backup, confirm=True)
    assert _rep_count(d, seed["office1"]) == before  # live DB untouched
