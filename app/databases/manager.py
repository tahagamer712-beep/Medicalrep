"""Tenant manager: owns every database connection in the system.

Rules:
- The ONLY way to reach an office database is through this module.
- The office is resolved from the identity DB (never from user-supplied text).
- SQLite pragmas: WAL mode + busy timeout + foreign keys, set on each connect.
- Office schema upgrades run here, transactionally, per file (see
  docs/DATABASE.md for why Alembic is deferred for office files).
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.core.errors import NotFound
from app.core.time_utils import utcnow
from app.databases import office_models as om
from app.identity import models as im

_engines: dict[str, object] = {}


def _apply_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA busy_timeout=5000;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.close()


def _engine_for(path: Path):
    key = str(path)
    if key not in _engines:
        engine = create_engine(f"sqlite:///{path}", future=True)
        event.listens_for(engine, "connect")(_apply_pragmas)
        _engines[key] = engine
    return _engines[key]


def _drop_engine(path: Path):
    """Remove the cached engine for a path AND close its pooled connections.

    Popping without disposing leaks WAL connections: a leaked connection
    keeps the (possibly deleted) -shm mmapped, and new connections then see
    an inconsistent WAL view -> "disk I/O error". Always use this instead of
    bare _engines.pop().
    """
    eng = _engines.pop(str(path), None)
    if eng is not None:
        eng.dispose()
    return eng


def identity_db_path(data_dir: Path) -> Path:
    return data_dir / "identity.db"


def office_db_path(data_dir: Path, office_id: int) -> Path:
    return data_dir / f"office_{office_id}.db"


@contextmanager
def identity_session(data_dir: Path):
    """Yield a transactional session on the central identity DB."""
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = _engine_for(identity_db_path(data_dir))
    # expire_on_commit=False: returned objects stay usable after commit,
    # avoiding DetachedInstanceError in short-lived service sessions.
    s = Session(engine, expire_on_commit=False)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def resolve_office(data_dir: Path, office_id: int) -> Path:
    """Return the office DB path as recorded in the identity DB.

    Raises NotFound if the office (or its db_path) is unknown — callers can
    never invent a path.
    """
    with identity_session(data_dir) as s:
        office = s.get(im.Office, office_id)
        if office is None or not office.db_path:
            raise NotFound("office database not provisioned")
        return Path(office.db_path)


@contextmanager
def office_session(data_dir: Path, office_id: int):
    """Yield a transactional session on the office's OWN database file."""
    path = resolve_office(data_dir, office_id)
    if not path.exists():
        raise NotFound("office database file missing")
    engine = _engine_for(path)
    s = Session(engine, expire_on_commit=False)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def provision_office_db(data_dir: Path, office_id: int) -> Path:
    """Create the office database file + schema. Idempotent: safe to re-run."""
    path = office_db_path(data_dir, office_id)
    data_dir.mkdir(parents=True, exist_ok=True)
    engine = _engine_for(path)
    om.OfficeBase.metadata.create_all(engine)
    with Session(engine) as s:
        if s.query(om.SchemaVersion).count() == 0:
            s.add(om.SchemaVersion(version=om.OFFICE_SCHEMA_VERSION))
            s.commit()
    return path


def migrate_office_db(data_dir: Path, office_id: int) -> int:
    """Apply pending office-schema upgrades for one office file.

    Atomicity is at FILE level, not transaction level: pysqlite issues an
    implicit COMMIT before DDL statements (verified empirically), so a
    transaction rollback cannot undo partial DDL. Instead we copy the file
    aside before upgrading and restore it on any failure. Upgrade functions
    must additionally be idempotent (CREATE TABLE IF NOT EXISTS style) so a
    retry after a crash is safe.
    Returns the resulting schema version.

    (Alembic is intentionally not used here: office DBs are dynamically
    provisioned per-tenant files, not a static database list.)
    """
    import shutil

    path = resolve_office(data_dir, office_id)
    engine = _engine_for(path)
    with Session(engine) as s:
        rows = s.query(om.SchemaVersion.version).all()
        current = max((v for (v,) in rows), default=0)
    pending = [t for t in sorted(_OFFICE_UPGRADES) if t > current]
    if not pending:
        return current
    # Checkpoint first: in WAL mode the newest data may live in the -wal file,
    # so a raw copy of the main file alone would be an inconsistent snapshot.
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("PRAGMA wal_checkpoint(TRUNCATE);"))
        conn.commit()
    bak = path.with_suffix(".db.migrate.bak")
    shutil.copy2(path, bak)
    try:
        for target in pending:
            with engine.begin() as conn:
                _OFFICE_UPGRADES[target](conn)
                if current == 0:
                    conn.execute(
                        om.SchemaVersion.__table__.insert().values(version=target)
                    )
                else:
                    conn.execute(
                        om.SchemaVersion.__table__.update().values(version=target)
                    )
            current = target
    except Exception:
        _drop_engine(path)  # close pooled connections holding the file
        shutil.copy2(bak, path)
        for suffix in ("-wal", "-shm", "-journal"):
            sibling = Path(str(path) + suffix)
            if sibling.exists():
                sibling.unlink()
        raise
    finally:
        if bak.exists():
            bak.unlink()
    return current


def _upgrade_office_to_2(conn) -> None:
    """v2: add backup_records table."""
    om.BackupRecord.__table__.create(bind=conn, checkfirst=True)


def _upgrade_office_to_3(conn) -> None:
    """v3: plans, visits, attendance, notification jobs."""
    for table in (
        om.Doctor.__table__,
        om.Pharmacy.__table__,
        om.WeeklyPlan.__table__,
        om.DailyPlan.__table__,
        om.PlanRevision.__table__,
        om.PlannedVisit.__table__,
        om.ActualVisit.__table__,
        om.AttendanceRecord.__table__,
        om.NotificationJob.__table__,
    ):
        table.create(bind=conn, checkfirst=True)


def _upgrade_office_to_4(conn) -> None:
    """v4: products, leave, meetings, holidays, surveys, attachments."""
    for table in (
        om.ProductUnit.__table__,
        om.Product.__table__,
        om.LeaveType.__table__,
        om.LeaveRequest.__table__,
        om.Meeting.__table__,
        om.MeetingAttendee.__table__,
        om.Holiday.__table__,
        om.Survey.__table__,
        om.SurveyQuestion.__table__,
        om.SurveyResponse.__table__,
        om.SurveyAnswer.__table__,
        om.Attachment.__table__,
    ):
        table.create(bind=conn, checkfirst=True)


def _upgrade_office_to_5(conn) -> None:
    """v5: payroll periods, salary records, financial transactions ledger."""
    for table in (
        om.PayrollPeriod.__table__,
        om.SalaryRecord.__table__,
        om.FinancialTransaction.__table__,
    ):
        table.create(bind=conn, checkfirst=True)


def _upgrade_office_to_6(conn) -> None:
    """v6: doctor profile enrichment + salary history + import logs.

    Doctors table gets new columns via ALTER (SQLite supports ADD COLUMN).
    Existing doctors are backfilled with a stable external_code.
    """
    import uuid as _uuid

    cols = [
        ("sub_area", "VARCHAR(100) DEFAULT ''"),
        ("nearest_pharmacy_id", "INTEGER"),
        ("doctor_class", "VARCHAR(10) DEFAULT ''"),
        ("additional_info", "TEXT DEFAULT ''"),
        ("reception_days_json", "TEXT DEFAULT '[]'"),
        ("non_reception_days_json", "TEXT DEFAULT '[]'"),
        ("reception_time", "VARCHAR(100) DEFAULT ''"),
        ("doctor_notes", "TEXT DEFAULT ''"),
        ("dealer_info", "TEXT DEFAULT ''"),
        ("assigned_rep_id", "INTEGER"),
        ("external_code", "VARCHAR(32)"),
        ("version", "INTEGER DEFAULT 1"),
        ("updated_at", "DATETIME"),
    ]
    existing = {r[1] for r in
                conn.exec_driver_sql("PRAGMA table_info(doctors)").fetchall()}
    for name, ddl in cols:
        if name not in existing:
            conn.exec_driver_sql(
                f"ALTER TABLE doctors ADD COLUMN {name} {ddl}")
    # backfill stable external codes for pre-v6 doctors
    rows = conn.exec_driver_sql(
        "SELECT id FROM doctors WHERE external_code IS NULL").fetchall()
    for (did,) in rows:
        conn.exec_driver_sql(
            "UPDATE doctors SET external_code = ? WHERE id = ?",
            (_uuid.uuid4().hex[:16], did))
    for table in (
        om.DoctorProduct.__table__,
        om.DoctorHistory.__table__,
        om.DoctorFieldSurvey.__table__,
        om.BaseSalaryChange.__table__,
        om.ImportLog.__table__,
    ):
        table.create(bind=conn, checkfirst=True)


def _upgrade_office_to_7(conn) -> None:
    """v7: soft-delete columns on attachments.

    Follows the v6 pattern: check PRAGMA table_info first so a retry after
    a crash is idempotent (plain ALTER TABLE ADD COLUMN would fail twice).
    """
    existing = {r[1] for r in
                conn.exec_driver_sql("PRAGMA table_info(attachments)").fetchall()}
    cols = [
        ("deleted", "BOOLEAN DEFAULT 0"),
        ("deleted_at", "DATETIME"),
    ]
    for name, ddl in cols:
        if name not in existing:
            conn.exec_driver_sql(
                f"ALTER TABLE attachments ADD COLUMN {name} {ddl}")


def _upgrade_office_to_8(conn) -> None:
    """v8: catalog product links (visit_products, field_survey_products)."""
    for table in (
        om.VisitProduct.__table__,
        om.FieldSurveyProduct.__table__,
    ):
        table.create(bind=conn, checkfirst=True)


_OFFICE_UPGRADES = {
    2: _upgrade_office_to_2,
    3: _upgrade_office_to_3,
    4: _upgrade_office_to_4,
    5: _upgrade_office_to_5,
    6: _upgrade_office_to_6,
    7: _upgrade_office_to_7,
    8: _upgrade_office_to_8,
}


# --- backup / restore -------------------------------------------------------

def _keys_dir(data_dir: Path) -> Path:
    d = data_dir / "office_keys"
    d.mkdir(parents=True, exist_ok=True)
    return d


def office_key(data_dir: Path, office_id: int) -> bytes:
    """Per-office encryption key. Generated once, stored 0600, never next to backups."""
    key_file = _keys_dir(data_dir) / f"office_{office_id}.key"
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o600)
    return key_file.read_bytes()


def backup_office(data_dir: Path, office_id: int) -> Path:
    """Encrypted backup of one office DB. Returns the backup file path.

    Immediately verifies the backup by decrypting it in memory and running
    an integrity check — a corrupt key or write is caught here, not months
    later at restore time. The backup is logged in backup_records.
    """
    import hashlib

    src = resolve_office(data_dir, office_id)
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = utcnow().strftime("%Y%m%dT%H%M%S")
    tmp = backup_dir / f"office_{office_id}_{ts}.db.tmp"
    # sqlite backup API: consistent snapshot even while DB is in use
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(tmp)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    raw = tmp.read_bytes()
    tmp.unlink()
    key = office_key(data_dir, office_id)
    token = Fernet(key).encrypt(raw)
    out = backup_dir / f"office_{office_id}_{ts}.enc"
    out.write_bytes(token)
    out.chmod(0o600)

    # verify now: decrypt in memory + integrity check on a temp copy
    check_tmp = backup_dir / f"office_{office_id}_{ts}.verify.tmp"
    verified = 0
    try:
        plain = Fernet(key).decrypt(token)
        check_tmp.write_bytes(plain)
        conn = sqlite3.connect(check_tmp)
        try:
            verified = 1 if conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok" else 0
        finally:
            conn.close()
    finally:
        if check_tmp.exists():
            check_tmp.unlink()
    if not verified:
        out.unlink()
        raise ValueError("backup verification failed — backup discarded")

    digest = hashlib.sha256(token).hexdigest()
    with office_session(data_dir, office_id) as s:
        s.add(om.BackupRecord(file_name=out.name, sha256=digest,
                              size_bytes=len(token), encrypted=1, verified=1))
    return out



def _safety_snapshot(data_dir: Path, live_file: Path) -> Path | None:
    """Copy the current live DB to backups/safety/<timestamp>/ before a
    restore replaces it. Gives a way back if the restore was a mistake."""
    if not live_file.exists():
        return None
    from datetime import datetime, timezone
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest_dir = Path(data_dir) / "backups" / "safety" / stamp
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / live_file.name
    import shutil
    shutil.copy2(live_file, dest)
    return dest


def restore_office(data_dir: Path, office_id: int, backup_file: Path, *, confirm: bool = False) -> Path:
    """Restore an office DB from an encrypted backup.

    Requires confirm=True (never overwrite live data silently). Verifies
    integrity before replacing.
    """
    if not confirm:
        raise ValueError("restore requires confirm=True")
    raw = Fernet(office_key(data_dir, office_id)).decrypt(backup_file.read_bytes())
    dst = resolve_office(data_dir, office_id)
    tmp = dst.with_suffix(".restore.tmp")
    tmp.write_bytes(raw)
    try:
        conn = sqlite3.connect(tmp)
        try:
            ok = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        ok = "corrupt"  # not even a readable SQLite file
    if ok != "ok":
        tmp.unlink()
        raise ValueError("backup integrity check failed — not restored")
    _safety_snapshot(data_dir, dst)
    tmp.replace(dst)
    # drop cached engine so the next session re-opens the replaced file
    _drop_engine(dst)  # pooled connections may hold the old inode
    # stale WAL/SHM from the live DB must go, or SQLite would replay them
    # over the restored file and resurrect pre-restore state
    for suffix in ("-wal", "-shm", "-journal"):
        sibling = Path(str(dst) + suffix)
        if sibling.exists():
            sibling.unlink()
    return dst


# --- identity backup / restore -----------------------------------------------
# The central identity.db holds users, companies, offices, memberships and
# the central audit log. Office backups alone are NOT enough: without
# identity.db the office files cannot be mapped. Both must be backed up
# together (a "full backup" = identity + every office).

def identity_key(data_dir: Path) -> bytes:
    """Encryption key for identity backups. Generated once, stored 0600."""
    key_file = _keys_dir(data_dir) / "identity.key"
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
        key_file.chmod(0o600)
    return key_file.read_bytes()


def backup_identity(data_dir: Path) -> Path:
    """Encrypted backup of the central identity DB.

    Uses the sqlite backup API (WAL-safe, consistent even mid-transaction),
    never a raw file copy. Verifies by decrypting in memory + integrity
    check before returning. Logged as a central audit event.
    """
    import hashlib

    src = identity_db_path(data_dir)
    if not src.exists():
        raise NotFound("identity database missing")
    backup_dir = data_dir / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = utcnow().strftime("%Y%m%dT%H%M%S")
    tmp = backup_dir / f"identity_{ts}.db.tmp"
    src_conn = sqlite3.connect(src)
    dst_conn = sqlite3.connect(tmp)
    try:
        src_conn.backup(dst_conn)
    finally:
        dst_conn.close()
        src_conn.close()
    raw = tmp.read_bytes()
    tmp.unlink()
    key = identity_key(data_dir)
    token = Fernet(key).encrypt(raw)
    out = backup_dir / f"identity_{ts}.enc"
    out.write_bytes(token)
    out.chmod(0o600)

    check_tmp = backup_dir / f"identity_{ts}.verify.tmp"
    verified = 0
    try:
        plain = Fernet(key).decrypt(token)
        check_tmp.write_bytes(plain)
        conn = sqlite3.connect(check_tmp)
        try:
            verified = 1 if conn.execute("PRAGMA integrity_check;").fetchone()[0] == "ok" else 0
        finally:
            conn.close()
    finally:
        if check_tmp.exists():
            check_tmp.unlink()
    if not verified:
        out.unlink()
        raise ValueError("identity backup verification failed — discarded")

    digest = hashlib.sha256(token).hexdigest()
    with identity_session(data_dir) as s:
        s.add(im.CentralAuditEvent(
            actor_user_id=None, event_type="IDENTITY_BACKUP_CREATED",
            entity="identity_backup", result="ok",
            reason=f"{out.name} sha256={digest[:16]}"))
    return out


def restore_identity(data_dir: Path, backup_file: Path, *,
                     confirm: bool = False) -> Path:
    """Restore identity.db from an encrypted backup. Requires confirm=True.

    After replacing the file, office db_paths are re-pointed to the current
    data_dir: a backup taken on another machine may carry absolute paths
    that do not exist here. Offices themselves are restored separately
    (restore_office per office) — restore identity FIRST, then offices,
    all from backups taken at the same time.
    """
    if not confirm:
        raise ValueError("restore requires confirm=True")
    raw = Fernet(identity_key(data_dir)).decrypt(backup_file.read_bytes())
    dst = identity_db_path(data_dir)
    tmp = dst.with_suffix(".restore.tmp")
    tmp.write_bytes(raw)
    try:
        conn = sqlite3.connect(tmp)
        try:
            ok = conn.execute("PRAGMA integrity_check;").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error:
        ok = "corrupt"  # not even a readable SQLite file
    if ok != "ok":
        tmp.unlink()
        raise ValueError("backup integrity check failed — not restored")
    _safety_snapshot(data_dir, dst)
    tmp.replace(dst)
    _drop_engine(dst)
    for suffix in ("-wal", "-shm", "-journal"):
        sibling = Path(str(dst) + suffix)
        if sibling.exists():
            sibling.unlink()
    # re-point office db paths to THIS data_dir (portable restores)
    with identity_session(data_dir) as s:
        for o in s.query(im.Office).all():
            o.db_path = str(office_db_path(data_dir, o.id))
    return dst


# --- backup sets: consistent identity + offices --------------------------------
# A single backup.py --all run writes a manifest tying the files together.
# Restoring from a manifest verifies the set BEFORE touching any live DB:
# every file present, checksums match, timestamps within tolerance.
# Each DB restore is verified-before-replace, so a failed office restore
# leaves that DB untouched and the operator can retry just that office.

SET_TIMESTAMP_TOLERANCE_SECONDS = 3600


def _sha256_file(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_backup_manifest(data_dir: Path, identity_file: Path,
                          office_files: dict[int, Path]) -> Path:
    """Write a manifest for a backup set taken together. Returns the path."""
    import json
    backup_dir = data_dir / "backups"
    ts = utcnow().strftime("%Y%m%dT%H%M%S")
    manifest = {
        "created_at": ts,
        "identity": {"file": identity_file.name,
                     "sha256": _sha256_file(identity_file)},
        "offices": [{"id": oid, "file": p.name, "sha256": _sha256_file(p)}
                    for oid, p in sorted(office_files.items())],
    }
    out = backup_dir / f"manifest_{ts}.json"
    out.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    out.chmod(0o600)
    return out


def verify_backup_set(data_dir: Path, manifest_path: Path) -> dict:
    """Verify a backup set WITHOUT touching live data. Raises ValueError
    describing the first problem found. Returns the manifest on success."""
    import json
    from datetime import datetime
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    backup_dir = data_dir / "backups"
    problems: list[str] = []

    def check(entry: dict, label: str):
        p = backup_dir / entry["file"]
        if not p.exists():
            problems.append(f"{label}: missing file {entry['file']}")
            return
        if _sha256_file(p) != entry["sha256"]:
            problems.append(f"{label}: checksum mismatch {entry['file']}")

    check(manifest["identity"], "identity")
    for o in manifest.get("offices", []):
        check(o, f"office {o['id']}")

    # timestamps must be close: files from the same --all run
    try:
        def ts_of(name: str) -> datetime:
            core = name.rsplit(".enc", 1)[0].rsplit("_", 1)[-1]
            return datetime.strptime(core, "%Y%m%dT%H%M%S")
        stamps = [ts_of(manifest["identity"]["file"])]
        stamps += [ts_of(o["file"]) for o in manifest.get("offices", [])]
        span = (max(stamps) - min(stamps)).total_seconds()
        if span > SET_TIMESTAMP_TOLERANCE_SECONDS:
            problems.append(
                f"backup files span {int(span)}s — not from one run "
                f"(tolerance {SET_TIMESTAMP_TOLERANCE_SECONDS}s)")
    except (ValueError, IndexError):
        problems.append("unparseable timestamp in backup file names")

    if problems:
        raise ValueError("backup set invalid: " + "; ".join(problems))
    return manifest


def restore_backup_set(data_dir: Path, manifest_path: Path, *,
                       confirm: bool = False) -> dict:
    """Restore a full set: verify first, then identity, then each office.

    Returns {"restored": [...], "failed": [...]}. A failed office restore
    raises after attempting all offices; already-restored DBs stay restored
    (each restore is verified-before-replace, so retrying the failed ones
    is safe).
    """
    manifest = verify_backup_set(data_dir, manifest_path)
    if not confirm:
        raise ValueError("restore requires confirm=True")
    backup_dir = data_dir / "backups"
    restored: list[str] = []
    failed: list[str] = []
    restore_identity(data_dir, backup_dir / manifest["identity"]["file"],
                     confirm=True)
    restored.append("identity")
    for o in manifest.get("offices", []):
        label = f"office {o['id']}"
        try:
            restore_office(data_dir, o["id"], backup_dir / o["file"],
                           confirm=True)
            restored.append(label)
        except Exception as e:
            failed.append(f"{label}: {type(e).__name__}: {e}")
    if failed:
        raise ValueError("backup set partially restored. restored=%s failed=%s"
                         % (restored, failed))
    return {"restored": restored, "failed": failed}
