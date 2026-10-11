"""Alembic manages the identity schema from scratch."""
import os
from pathlib import Path

from sqlalchemy import inspect

from app.databases import manager as dbm


def test_alembic_upgrade_head_creates_identity_schema(tmp_path, monkeypatch):
    from tests.conftest import PROJECT_ROOT, _upgrade_identity
    monkeypatch.chdir(PROJECT_ROOT)
    d = tmp_path / "fresh"
    d.mkdir()
    _upgrade_identity(d)
    engine = dbm._engine_for(dbm.identity_db_path(d))
    tables = set(inspect(engine).get_table_names())
    assert {"companies", "offices", "users", "memberships",
            "central_audit_events", "alembic_version"} <= tables
    os.environ["MEDREP_DATA_DIR"] = str(d)  # keep env consistent
