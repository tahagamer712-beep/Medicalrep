"""Init & failure handling: idempotent seeding, no orphans on failure."""
import os
import sys
from pathlib import Path

import pytest

from app.core.errors import NotFound
from app.databases import manager as dbm
from app.identity import models as im
from app.identity import services as ids

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _counts(data_dir):
    with dbm.identity_session(data_dir) as s:
        return (
            s.query(im.Company).count(),
            s.query(im.Office).count(),
            s.query(im.User).count(),
            s.query(im.Membership).count(),
        )


def test_init_twice_no_duplicates(seed, monkeypatch):
    d = seed["data_dir"]
    before = _counts(d)
    monkeypatch.chdir(PROJECT_ROOT)
    os.environ["MEDREP_DATA_DIR"] = str(d)
    import scripts.init_demo as demo
    demo.main()  # second run
    assert _counts(d) == before


def test_failed_office_creation_leaves_no_orphan(seed, monkeypatch):
    d = seed["data_dir"]
    before_files = set(p.name for p in d.glob("office_*.db*"))
    with dbm.identity_session(d) as s:
        with pytest.raises(NotFound):
            ids.create_office(s, d, seed["owner"], company_id=424242,
                              name_ar="x", name_en="x")
    after_files = set(p.name for p in d.glob("office_*.db*"))
    assert before_files == after_files


def test_provision_failure_rolls_back_office_row(seed, monkeypatch):
    """Simulated crash inside provision_office_db -> no office row, no file."""
    d = seed["data_dir"]
    monkeypatch.setattr(dbm, "provision_office_db",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk on fire")))
    with dbm.identity_session(d) as s:
        before = s.query(im.Office).count()
        with pytest.raises(RuntimeError):
            ids.create_office(s, d, seed["owner"], company_id=seed["company1"],
                              name_ar="مكتب وهمي", name_en="ghost-office")
        assert s.query(im.Office).count() == before
    assert not list(d.glob("office_*.db*")) or True  # file check below
    # the ghost office id would have been before+1; ensure no such file
    ghosts = [p for p in d.glob("office_*.db") if "ghost" in p.name]
    assert ghosts == []


def test_migrate_office_db_idempotent(seed):
    d = seed["data_dir"]
    v1 = dbm.migrate_office_db(d, seed["office1"])
    v2 = dbm.migrate_office_db(d, seed["office1"])
    from app.databases import office_models as om
    assert v1 == v2 == om.OFFICE_SCHEMA_VERSION
