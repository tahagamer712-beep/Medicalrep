"""Alembic environment — manages the CENTRAL identity database only.

Office databases are provisioned per-tenant at runtime (see
app/databases/manager.py); they are intentionally NOT managed by Alembic
(documented in docs/DATABASE.md).
"""
import os
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, event

from app.identity.models import IdentityBase

config = context.config


def _db_url() -> str:
    # Read at call time (not import time) so tests can point at temp dirs.
    data_dir = Path(os.environ.get("MEDREP_DATA_DIR", "./data"))
    data_dir.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{data_dir / 'identity.db'}"


def _pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL;")
    cur.execute("PRAGMA busy_timeout=5000;")
    cur.execute("PRAGMA foreign_keys=ON;")
    cur.close()


target_metadata = IdentityBase.metadata


def run_migrations_offline() -> None:
    context.configure(url=_db_url(), target_metadata=target_metadata,
                      literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_db_url(), future=True)
    event.listens_for(engine, "connect")(_pragmas)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
