"""Reminders: DB-backed scheduling, idempotent ensure, retry, no duplicates.

We test the scheduling/send LOGIC only (FakeNotifier) — no claim is made
about real Telegram delivery.
"""
from datetime import datetime, timezone

import pytest

from app.databases import manager as dbm
from app.databases import office_models as om
from app.jobs import reminders
from app.jobs.notifier import FakeNotifier

# Sunday 2026-10-11 09:00 UTC = 12:00 Baghdad
NOW = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)


def _jobs(data_dir, oid, status=None):
    with dbm.office_session(data_dir, oid) as s:
        q = s.query(om.NotificationJob)
        if status:
            q = q.filter_by(status=status)
        return q.all()


def test_ensure_weekly_is_idempotent(seed):
    d = seed["data_dir"]
    oid = seed["office1"]
    n1 = reminders.ensure_weekly_reminder_jobs(d, oid, NOW)
    n2 = reminders.ensure_weekly_reminder_jobs(d, oid, NOW)
    assert n1 == 2  # two active reps in office 1
    assert n2 == 0  # second run creates nothing
    jobs = _jobs(d, oid)
    assert len({j.job_key for j in jobs}) == len(jobs)  # keys unique
    # fires Friday 16:00 Baghdad = 13:00 UTC
    assert jobs[0].scheduled_for.utcoffset().total_seconds() == 0
    assert jobs[0].scheduled_for.hour == 13
    assert jobs[0].scheduled_for.weekday() == 4


def test_ensure_daily_respects_work_days(seed):
    d = seed["data_dir"]
    oid = seed["office1"]
    n = reminders.ensure_daily_reminder_jobs(d, oid, NOW, days_ahead=7)
    assert n > 0
    n2 = reminders.ensure_daily_reminder_jobs(d, oid, NOW, days_ahead=7)
    assert n2 == 0
    # all jobs fire 1h before 08:00 Baghdad = 04:00 UTC
    for j in _jobs(d, oid, status="pending"):
        if j.kind == "daily_plan_reminder":
            assert (j.scheduled_for.hour, j.scheduled_for.minute) == (4, 0)


def test_run_due_sends_once(seed):
    d = seed["data_dir"]
    oid = seed["office1"]
    reminders.ensure_weekly_reminder_jobs(d, oid, NOW)
    notifier = FakeNotifier()
    # not due yet
    stats = reminders.run_due_jobs(d, NOW, notifier)
    assert stats["sent"] == 0
    # jump past the Friday 16:00 Baghdad fire time
    later = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
    stats = reminders.run_due_jobs(d, later, notifier)
    assert stats["sent"] == 2
    assert sum(len(v) for v in notifier.inbox.values()) == 2
    # "restart": second run sends nothing again
    stats = reminders.run_due_jobs(d, later, FakeNotifier())
    assert stats["sent"] == 0
    assert all(j.status == "sent" for j in _jobs(d, oid))


def test_failed_send_retries_then_fails(seed):
    d = seed["data_dir"]
    oid = seed["office1"]
    reminders.ensure_weekly_reminder_jobs(d, oid, NOW)
    later = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
    notifier = FakeNotifier()
    notifier.fail_next = 99  # always fail
    for _ in range(2):
        stats = reminders.run_due_jobs(d, later, notifier)
        assert stats["sent"] == 0
    jobs = _jobs(d, oid)
    assert all(j.status == "pending" and j.attempts == 2 for j in jobs)
    stats = reminders.run_due_jobs(d, later, notifier)  # 3rd attempt
    assert stats["failed"] == 2
    assert all(j.status == "failed" for j in _jobs(d, oid))
    # failed jobs are never retried again
    stats = reminders.run_due_jobs(d, later, notifier)
    assert stats["failed"] == 0 and stats["sent"] == 0


def test_send_exception_counts_as_retry(seed):
    d = seed["data_dir"]
    oid = seed["office1"]
    reminders.ensure_weekly_reminder_jobs(d, oid, NOW)
    later = datetime(2026, 10, 16, 14, 0, tzinfo=timezone.utc)
    notifier = FakeNotifier()
    notifier.raise_next = 2  # first two sends raise
    stats = reminders.run_due_jobs(d, later, notifier)
    assert stats["retried"] == 2
    stats = reminders.run_due_jobs(d, later, notifier)
    assert stats["sent"] == 2  # recovered on retry


def test_jobs_are_per_office(seed):
    d = seed["data_dir"]
    reminders.ensure_weekly_reminder_jobs(d, seed["office1"], NOW)
    reminders.ensure_weekly_reminder_jobs(d, seed["office2"], NOW)
    assert len(_jobs(d, seed["office1"])) == 2
    assert len(_jobs(d, seed["office2"])) == 1  # one rep in office 2
