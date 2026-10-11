"""Identity backup/restore: encrypted, verified, confirm-gated, portable.

All tests run on the isolated seed fixture dir — never the project's data/.
"""
import pytest

from app.core.errors import NotFound
from app.databases import manager as dbm
from app.identity import models as im


def test_backup_identity_encrypted_and_verified(seed):
    d = seed["data_dir"]
    out = dbm.backup_identity(d)
    assert out.exists() and out.suffix == ".enc"
    assert out.stat().st_mode & 0o777 == 0o600
    # encrypted: not a SQLite file
    assert out.read_bytes()[:6] != b"SQLite"
    # key stored separately, 0600
    key = d / "office_keys" / "identity.key"
    assert key.exists() and key.stat().st_mode & 0o777 == 0o600
    # audit event logged
    with dbm.identity_session(d) as s:
        ev = (s.query(im.CentralAuditEvent)
              .filter_by(event_type="IDENTITY_BACKUP_CREATED").one())
        assert ev.result == "ok"


def test_restore_identity_roundtrip(seed):
    d = seed["data_dir"]
    out = dbm.backup_identity(d)
    with dbm.identity_session(d) as s:
        users_before = s.query(im.User).count()
    # simulate divergence after backup: add a user
    with dbm.identity_session(d) as s:
        s.add(im.User(full_name="مستخدم بعد النسخة", phone="07999999999"))
    with dbm.identity_session(d) as s:
        assert s.query(im.User).count() == users_before + 1
    # restore requires explicit confirm
    with pytest.raises(ValueError):
        dbm.restore_identity(d, out, confirm=False)
    with dbm.identity_session(d) as s:
        assert s.query(im.User).count() == users_before + 1  # untouched
    dbm.restore_identity(d, out, confirm=True)
    with dbm.identity_session(d) as s:
        assert s.query(im.User).count() == users_before
        assert (s.query(im.User)
                .filter_by(phone="07999999999").first() is None)


def test_restore_identity_repoints_office_paths(seed, tmp_path):
    """A backup restored under a different data_dir gets working office paths."""
    d = seed["data_dir"]
    out = dbm.backup_identity(d)
    # restore into a FRESH dir (simulates a new machine)
    new_dir = tmp_path / "newdata"
    new_dir.mkdir()
    (new_dir / "backups").mkdir()
    import shutil
    shutil.copy(out, new_dir / "backups" / out.name)
    # identity key must travel with the backup (operator copies office_keys/)
    (new_dir / "office_keys").mkdir()
    shutil.copy(d / "office_keys" / "identity.key",
                new_dir / "office_keys" / "identity.key")
    dbm.restore_identity(new_dir, new_dir / "backups" / out.name, confirm=True)
    with dbm.identity_session(new_dir) as s:
        for o in s.query(im.Office).all():
            assert o.db_path == str(new_dir / f"office_{o.id}.db")


def test_backup_identity_missing_db(tmp_path):
    with pytest.raises(NotFound):
        dbm.backup_identity(tmp_path / "empty")


def test_restore_identity_wrong_key_fails_cleanly(seed, tmp_path):
    """Key not copied to the new machine: a fresh key is generated, decrypt
    fails with InvalidToken, and the live DB is untouched."""
    from cryptography.fernet import InvalidToken
    d = seed["data_dir"]
    out = dbm.backup_identity(d)
    new_dir = tmp_path / "nokey"
    (new_dir / "backups").mkdir(parents=True)
    import shutil
    shutil.copy(out, new_dir / "backups" / out.name)
    # identity.db must exist for restore target; seed a fresh one
    dbm.identity_db_path(new_dir).parent.mkdir(parents=True, exist_ok=True)
    import sqlite3
    sqlite3.connect(dbm.identity_db_path(new_dir)).close()
    with pytest.raises(InvalidToken):
        dbm.restore_identity(new_dir, new_dir / "backups" / out.name,
                             confirm=True)


def test_restore_identity_corrupt_file_leaves_db_untouched(seed, tmp_path):
    from cryptography.fernet import InvalidToken
    d = seed["data_dir"]
    out = dbm.backup_identity(d)
    bad = tmp_path / "bad.enc"
    data = bytearray(out.read_bytes())
    data[len(data) // 2] ^= 0xFF  # flip bits in the middle
    bad.write_bytes(bytes(data))
    with dbm.identity_session(d) as s:
        users_before = s.query(im.User).count()
    with pytest.raises(InvalidToken):
        dbm.restore_identity(d, bad, confirm=True)
    with dbm.identity_session(d) as s:
        assert s.query(im.User).count() == users_before


def test_restore_identity_integrity_failure_midway(seed, tmp_path):
    """Valid encryption but garbage content: integrity check fails BEFORE
    the live file is replaced — the DB is never half-restored."""
    from cryptography.fernet import Fernet
    d = seed["data_dir"]
    dbm.backup_identity(d)  # ensures identity.key exists
    fake = tmp_path / "fake.enc"
    fake.write_bytes(Fernet(dbm.identity_key(d)).encrypt(b"not a database"))
    with dbm.identity_session(d) as s:
        users_before = s.query(im.User).count()
    with pytest.raises(ValueError, match="integrity check failed"):
        dbm.restore_identity(d, fake, confirm=True)
    with dbm.identity_session(d) as s:
        assert s.query(im.User).count() == users_before
