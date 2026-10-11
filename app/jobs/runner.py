"""APScheduler wiring: ticks the DB-backed jobs.

- Every minute: run due jobs.
- Every 30 minutes: ensure future reminder jobs exist.
All state is in the DB; the scheduler itself holds nothing, so stopping and
restarting never duplicates reminders (job_key UNIQUE + status machine).

The notifier used here is LoggingNotifier: jobs are marked sent only when
the notifier confirms. Real Telegram delivery is NOT wired yet — the runner
logs instead of sending. We never claim a message was delivered.
"""
from __future__ import annotations

import logging
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler

from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.identity import models as im
from app.jobs import reminders
from app.jobs.notifier import Notifier

logger = logging.getLogger("job_runner")


class LoggingNotifier:
    """Notifier that logs instead of sending. Used until real Telegram
    delivery is wired and tested. Returns True (logged = handled)."""

    def send(self, user_id: int, text: str) -> bool:
        logger.info("reminder for user %s: %s", user_id, text)
        return True


_runners: dict[str, BackgroundScheduler] = {}


def _tick_run(data_dir: Path, notifier: Notifier) -> None:
    reminders.run_due_jobs(data_dir, utcnow(), notifier)


def _tick_ensure(data_dir: Path) -> None:
    now = utcnow()
    with dbm.identity_session(data_dir) as s:
        office_ids = [o.id for o in s.query(im.Office).all()]
    for oid in office_ids:
        try:
            reminders.ensure_weekly_reminder_jobs(data_dir, oid, now)
            reminders.ensure_daily_reminder_jobs(data_dir, oid, now)
        except Exception:
            continue  # one bad office must not stop the others


def start_runner(data_dir: Path, notifier: Notifier | None = None
               ) -> BackgroundScheduler:
    """Start the job runner. Idempotent per data_dir: calling twice returns
    the already-running scheduler instead of starting a second one
    (which would double-tick jobs)."""
    key = str(data_dir)
    existing = _runners.get(key)
    if existing is not None and existing.running:
        logger.info("job runner already running for %s", key)
        return existing
    notifier = notifier or LoggingNotifier()
    scheduler = BackgroundScheduler()
    scheduler.add_job(lambda: _tick_run(data_dir, notifier),
                      "interval", minutes=1, id="run_due_jobs",
                      max_instances=1, coalesce=True)
    scheduler.add_job(lambda: _tick_ensure(data_dir),
                      "interval", minutes=30, id="ensure_jobs",
                      max_instances=1, coalesce=True)
    scheduler.start()
    _runners[key] = scheduler
    logger.info("job runner started for %s", key)
    return scheduler


def stop_runner(data_dir: Path) -> None:
    """Stop the runner for a data_dir. Safe to call when not running."""
    key = str(data_dir)
    scheduler = _runners.pop(key, None)
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("job runner stopped for %s", key)
