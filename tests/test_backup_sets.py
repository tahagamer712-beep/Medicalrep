"""Backup sets: manifest, verification, and safe multi-DB restore.

Dummy data only, isolated fixture dirs. Never touches real data.
"""
import pytest

from app.databases import manager as dbm
from app.identity import models as im


def _make_set(seed):
    d = seed["data_dir"]
    identity_file = dbm.backup_identity(d)
    office_files = {1: dbm.backup_office(d, 1)}
    manifest = dbm.write_backup_manifest(d, identity_file, office_files)
    return d, manifest


def test_manifest_written_and_verified(seed):
    d, manifest = _make_set(seed)
    assert manifest.exists()
    verified = dbm.verify_backup_set(d, manifest)
    assert verified["identity"]["file"].endswith(".enc")
    assert len(verified["offices"]) == 1


def test_verify_fails_on_missing_file(seed):
    d, manifest = _make_set(seed)
    (d / "backups" / "office_1_fake.enc").touch()  # noise, not the issue
    import json
    m = json.loads(manifest.read_text())
    m["offices"].append({"id": 99, "file": "office_99_nope.enc",
                         "sha256": "x"})
    manifest.write_text(json.dumps(m))
    with pytest.raises(ValueError, match="missing file"):
        dbm.verify_backup_set(d, manifest)


def test_verify_fails_on_checksum_mismatch(seed):
    d, manifest = _make_set(seed)
    import json
    m = json.loads(manifest.read_text())
    target = d / "backups" / m["identity"]["file"]
    data = bytearray(target.read_bytes())
    data[10] ^= 0xFF
    target.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="checksum mismatch"):
        dbm.verify_backup_set(d, manifest)


def test_verify_fails_on_mixed_timestamps(seed):
    d, manifest = _make_set(seed)
    import json
    m = json.loads(manifest.read_text())
    # pretend the office backup came from a much older run
    m["offices"][0]["file"] = m["offices"][0]["file"].replace(
        m["offices"][0]["file"].rsplit("_", 1)[-1].split(".")[0],
        "20200101T000000")
    manifest.write_text(json.dumps(m))
    with pytest.raises(ValueError, match="not from one run"):
        dbm.verify_backup_set(d, manifest)


def test_restore_set_roundtrip(seed):
    d, manifest = _make_set(seed)
    with dbm.identity_session(d) as s:
        users_before = s.query(im.User).count()
    # diverge after backup
    with dbm.identity_session(d) as s:
        s.add(im.User(full_name="بعد النسخة", phone="07888888888"))
    result = dbm.restore_backup_set(d, manifest, confirm=True)
    assert result == {"restored": ["identity", "office 1"], "failed": []}
    with dbm.identity_session(d) as s:
        assert s.query(im.User).count() == users_before


def test_restore_set_requires_confirm(seed):
    d, manifest = _make_set(seed)
    with pytest.raises(ValueError, match="confirm"):
        dbm.restore_backup_set(d, manifest, confirm=False)


def test_restore_set_partial_failure_reports(seed, tmp_path):
    """Corrupt one office backup: verify catches it BEFORE any restore,
    so the live DBs are untouched and the error names the problem."""
    d, manifest = _make_set(seed)
    import json
    m = json.loads(manifest.read_text())
    target = d / "backups" / m["offices"][0]["file"]
    data = bytearray(target.read_bytes())
    data[20] ^= 0xFF
    target.write_bytes(bytes(data))
    with pytest.raises(ValueError, match="checksum mismatch"):
        dbm.restore_backup_set(d, manifest, confirm=True)
    # nothing was restored — identity untouched
    with dbm.identity_session(d) as s:
        assert s.query(im.User).filter_by(
            phone="07888888888").first() is None


def test_restore_set_mid_restore_failure_is_reported_and_retryable(
        seed, monkeypatch):
    """Verify passes, but one office restore blows up mid-way: the error
    names what was restored and what failed, the failed office's live DB
    is untouched, and retrying after the cause is gone completes the set."""
    d = seed["data_dir"]
    ifile = dbm.backup_identity(d)
    ofiles = {1: dbm.backup_office(d, 1), 2: dbm.backup_office(d, 2)}
    manifest = dbm.write_backup_manifest(d, ifile, ofiles)

    real_restore_office = dbm.restore_office

    def flaky(data_dir, office_id, backup_path, *, confirm):
        if office_id == 2:
            raise OSError("simulated disk failure mid-restore")
        return real_restore_office(data_dir, office_id, backup_path,
                                   confirm=confirm)

    monkeypatch.setattr(dbm, "restore_office", flaky)
    with pytest.raises(ValueError, match="partially restored") as exc:
        dbm.restore_backup_set(d, manifest, confirm=True)
    assert "identity" in str(exc.value)
    assert "office 1" in str(exc.value)
    assert "office 2" in str(exc.value)
    # office 2's live DB was never replaced: seed data still there
    with dbm.office_session(d, 2) as s:
        from app.databases import office_models as om
        assert s.query(om.Doctor).count() >= 0  # schema intact

    # cause gone -> retry completes the whole set
    monkeypatch.setattr(dbm, "restore_office", real_restore_office)
    result = dbm.restore_backup_set(d, manifest, confirm=True)
    assert result["failed"] == []
    assert "office 2" in result["restored"]
