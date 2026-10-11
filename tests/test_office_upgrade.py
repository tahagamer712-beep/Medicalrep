"""Office schema upgrades: atomic, failure-safe, multi-office, version-compatible.

Covers condition 7: every office file carries schema_version, and upgrades are
tested — not just trusted.
"""
import sqlite3

import pytest
from sqlalchemy import inspect

from app.databases import manager as dbm
from app.databases import office_models as om


def _downgrade_to_v1(data_dir, office_id):
    """Simulate an old office file: drop v2 tables, stamp version 1."""
    path = dbm.office_db_path(data_dir, office_id)
    dbm._drop_engine(path)  # pop + dispose: no leaked WAL connections
    conn = sqlite3.connect(path)
    try:
        conn.execute("DROP TABLE IF EXISTS backup_records;")
        conn.execute("UPDATE schema_version SET version = 1;")
        conn.commit()
    finally:
        conn.close()


def _version(data_dir, office_id):
    with dbm.office_session(data_dir, office_id) as s:
        return s.query(om.SchemaVersion.version).one()[0]


def test_upgrade_v1_to_v2_preserves_data(seed):
    d = seed["data_dir"]
    oid = seed["office1"]
    _downgrade_to_v1(d, oid)
    assert _version(d, oid) == 1
    # seed data still there before upgrade
    with dbm.office_session(d, oid) as s:
        reps_before = s.query(om.Representative).count()
    v = dbm.migrate_office_db(d, oid)
    assert v == om.OFFICE_SCHEMA_VERSION
    assert _version(d, oid) == om.OFFICE_SCHEMA_VERSION
    engine = dbm._engine_for(dbm.office_db_path(d, oid))
    tables = inspect(engine).get_table_names()
    assert "backup_records" in tables
    assert "weekly_plans" in tables
    assert "actual_visits" in tables
    assert "attendance_records" in tables
    assert "notification_jobs" in tables
    with dbm.office_session(d, oid) as s:
        assert s.query(om.Representative).count() == reps_before


def test_upgrade_is_atomic_on_failure(seed, monkeypatch):
    """A failing upgrade rolls back the file and does NOT advance the version."""
    d = seed["data_dir"]
    oid = seed["office1"]
    _downgrade_to_v1(d, oid)

    latest = max(dbm._OFFICE_UPGRADES)
    real = dbm._OFFICE_UPGRADES[latest]

    def boom(conn):
        real(conn)
        raise RuntimeError("simulated crash mid-upgrade")

    monkeypatch.setitem(dbm._OFFICE_UPGRADES, latest, boom)
    with pytest.raises(RuntimeError):
        dbm.migrate_office_db(d, oid)
    # version stuck at 1, file restored
    assert _version(d, oid) == 1
    # retry with the real upgrade succeeds
    monkeypatch.setitem(dbm._OFFICE_UPGRADES, latest, real)
    assert dbm.migrate_office_db(d, oid) == om.OFFICE_SCHEMA_VERSION


def test_upgrade_multiple_offices(seed):
    d = seed["data_dir"]
    for oid in (seed["office1"], seed["office2"]):
        _downgrade_to_v1(d, oid)
    for oid in (seed["office1"], seed["office2"]):
        assert dbm.migrate_office_db(d, oid) == om.OFFICE_SCHEMA_VERSION
    # offices stay isolated: backup_records are per-file
    with dbm.office_session(d, seed["office1"]) as s1, \
         dbm.office_session(d, seed["office2"]) as s2:
        assert s1.query(om.BackupRecord).count() == 0
        assert s2.query(om.BackupRecord).count() == 0


def test_upgrade_already_current_is_noop(seed):
    d = seed["data_dir"]
    assert dbm.migrate_office_db(d, seed["office1"]) == om.OFFICE_SCHEMA_VERSION
    assert dbm.migrate_office_db(d, seed["office1"]) == om.OFFICE_SCHEMA_VERSION


def test_backup_writes_backup_record(seed):
    d = seed["data_dir"]
    oid = seed["office1"]
    out = dbm.backup_office(d, oid)
    with dbm.office_session(d, oid) as s:
        rec = s.query(om.BackupRecord).filter_by(file_name=out.name).one()
        assert rec.verified == 1
        assert rec.encrypted == 1
        assert rec.size_bytes == out.stat().st_size > 0
        assert len(rec.sha256) == 64


def _downgrade_to_v3(data_dir, office_id):
    """Simulate a v3 office file: drop v4/v5/v6 tables, stamp version 3."""
    path = dbm.office_db_path(data_dir, office_id)
    dbm._drop_engine(path)
    conn = sqlite3.connect(path)
    try:
        for t in ("product_units", "products", "leave_types", "leave_requests",
                  "meetings", "meeting_attendees", "holidays", "surveys",
                  "survey_questions", "survey_responses", "survey_answers",
                  "attachments", "payroll_periods", "salary_records",
                  "financial_transactions", "doctor_products", "doctor_history",
                  "doctor_field_surveys", "base_salary_changes", "import_logs"):
            conn.execute(f"DROP TABLE IF EXISTS {t};")
        conn.execute("UPDATE schema_version SET version = 3;")
        conn.commit()
    finally:
        conn.close()


def test_upgrade_v3_to_v6_preserves_doctors_and_backfills_codes(seed):
    from app.services import doctors as docs_svc
    d = seed["data_dir"]
    oid = seed["office1"]
    out = docs_svc.create_doctor(
        d, seed["supervisor1"], office_id=oid, name="د. قديم", area="المنصور")
    # simulate a pre-v6 row: NULL external code
    path = dbm.office_db_path(d, oid)
    dbm._drop_engine(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE doctors SET external_code = NULL WHERE id = ?",
                 (out["id"],))
    conn.commit()
    conn.close()

    _downgrade_to_v3(d, oid)
    assert _version(d, oid) == 3
    v = dbm.migrate_office_db(d, oid)
    assert v == om.OFFICE_SCHEMA_VERSION == 6

    with dbm.office_session(d, oid) as s:
        doc = s.get(om.Doctor, out["id"])
        assert doc.name == "د. قديم"  # data preserved
        assert doc.external_code  # backfilled, stable id present
        assert doc.version == 1
        # v6 tables exist
        assert s.query(om.DoctorProduct).count() == 0
        assert s.query(om.BaseSalaryChange).count() == 0
        assert s.query(om.ImportLog).count() == 0
        # v4/v5 tables restored too
        assert s.query(om.Product).count() >= 0
        assert s.query(om.FinancialTransaction).count() == 0
