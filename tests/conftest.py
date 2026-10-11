"""Shared fixtures: fresh temp data dir, alembic-upgraded identity DB, demo seed."""
import os
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _upgrade_identity(data_dir: Path) -> None:
    os.environ["MEDREP_DATA_DIR"] = str(data_dir)
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.upgrade(cfg, "head")


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    """Fresh data dir with identity schema (via alembic) + demo seed."""
    monkeypatch.chdir(PROJECT_ROOT)
    d = tmp_path / "data"
    d.mkdir()
    _upgrade_identity(d)
    os.environ["MEDREP_DATA_DIR"] = str(d)
    import scripts.init_demo as demo  # noqa: E402
    demo.main()
    return d


@pytest.fixture(autouse=True)
def _dispose_engines(data_dir):
    """Dispose cached engines after each test.

    dbm caches one engine per DB path forever; without disposal a full run
    leaks hundreds of pooled SQLite connections (FD exhaustion -> flaky
    OperationalErrors late in the suite). Production keeps a small stable
    set of paths, so the cache is fine there.
    """
    yield
    from app.databases import manager as dbm
    for key in [k for k in dbm._engines if str(data_dir) in k]:
        dbm._drop_engine(key)


@pytest.fixture()
def seed(data_dir):
    """Look up the demo ids created by init_demo."""
    from app.databases import manager as dbm
    from app.identity import models as im

    def uid(phone):
        with dbm.identity_session(data_dir) as s:
            return s.query(im.User).filter_by(phone=phone).one().id

    def office_id(company_name_en, office_name_en):
        with dbm.identity_session(data_dir) as s:
            c = s.query(im.Company).filter_by(name_en=company_name_en).one()
            o = (s.query(im.Office)
                 .filter_by(company_id=c.id, name_en=office_name_en).one())
            return c.id, o.id

    c1, o1 = office_id("alshifa", "baghdad-hq")
    c2, o2 = office_id("alrazi", "basra-office")
    return {
        "data_dir": data_dir,
        "owner": uid("07000000001"),
        "manager1": uid("07000000002"),
        "supervisor1": uid("07000000003"),
        "manager2": uid("07000000004"),
        "supervisor2": uid("07000000005"),
        "rep1_o1": uid("07000000011"),
        "rep2_o1": uid("07000000012"),
        "rep_o2": uid("07000000013"),
        "company1": c1, "office1": o1,
        "company2": c2, "office2": o2,
    }
