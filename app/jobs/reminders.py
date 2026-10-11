"""Reminder job management.

All scheduling state lives in each office's notification_jobs table —
never only in process memory. Restarts are safe: pending jobs are picked up,
sent jobs are never re-sent, failed jobs retry until max_attempts.

- Weekly plan reminder: Friday 16:00 office time, per active rep.
- Daily plan reminder: 1 hour before work_start on work days, per active rep.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.time_utils import ensure_utc, utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.jobs.notifier import Notifier


def _active_reps(data_dir: Path, office_id: int) -> list[int]:
    with dbm.identity_session(data_dir) as s:
        rows = (s.query(im.Membership)
                .filter_by(office_id=office_id, role="rep", status="active").all())
        return [m.user_id for m in rows]


def _office_tz_and_hours(data_dir: Path, office_id: int):
    with dbm.identity_session(data_dir) as s:
        o = s.get(im.Office, office_id)
        if o is None:
            raise ValueError("office not found")
        return o.timezone or "Asia/Baghdad", o.work_start or "08:00", \
            json.loads(o.work_days or "[6,0,1,2,3]")


def _next_friday_16h(tz_name: str, now: datetime) -> datetime:
    local = now.astimezone(ZoneInfo(tz_name))
    days_ahead = (4 - local.weekday()) % 7  # Monday=0 .. Friday=4
    target = (local + timedelta(days=days_ahead)).replace(
        hour=16, minute=0, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=7)
    return target.astimezone(timezone.utc)


def ensure_weekly_reminder_jobs(data_dir: Path, office_id: int,
                                now: datetime | None = None) -> int:
    """Idempotent: job_key UNIQUE guarantees no duplicates on re-run."""
    now = ensure_utc(now) if now else utcnow()
    tz_name, _, _ = _office_tz_and_hours(data_dir, office_id)
    fire_at = _next_friday_16h(tz_name, now)
    week_start = (fire_at.astimezone(ZoneInfo(tz_name)) -
                  timedelta(days=4)).strftime("%Y-%m-%d")  # Monday of that week
    created = 0
    for rep_id in _active_reps(data_dir, office_id):
        key = f"weekly_plan:{office_id}:{rep_id}:{week_start}"
        text = ("📋 تذكير: الخطة الأسبوعية للأسبوع القادم جاهزة للإرسال. "
                "أرسل خطتك قبل نهاية الدوام.")
        created += _ensure_job(data_dir, office_id, key, "weekly_plan_reminder",
                               fire_at, {"user_id": rep_id, "text": text})
    return created


def ensure_daily_reminder_jobs(data_dir: Path, office_id: int,
                               now: datetime | None = None,
                               days_ahead: int = 7) -> int:
    now = ensure_utc(now) if now else utcnow()
    tz_name, work_start, work_days = _office_tz_and_hours(data_dir, office_id)
    hh, mm = (int(x) for x in work_start.split(":"))
    tz = ZoneInfo(tz_name)
    local_today = now.astimezone(tz).date()
    created = 0
    for rep_id in _active_reps(data_dir, office_id):
        for d in range(days_ahead):
            day = local_today + timedelta(days=d)
            if day.weekday() not in work_days:
                continue
            fire_local = datetime(day.year, day.month, day.day, hh, mm,
                                  tzinfo=tz) - timedelta(hours=1)
            fire_at = fire_local.astimezone(timezone.utc)
            if fire_at <= now:
                continue  # already passed; don't create overdue jobs
            key = f"daily_plan:{office_id}:{rep_id}:{day.isoformat()}"
            text = ("🌅 تذكير: أرسل خطتك اليومية قبل النزول بساعة "
                    f"({work_start} بتوقيت المكتب).")
            created += _ensure_job(data_dir, office_id, key, "daily_plan_reminder",
                                   fire_at, {"user_id": rep_id, "text": text})
    return created


def _ensure_job(data_dir: Path, office_id: int, job_key: str, kind: str,
                fire_at: datetime, payload: dict) -> int:
    with dbm.office_session(data_dir, office_id) as osess:
        if osess.query(om.NotificationJob).filter_by(job_key=job_key).first():
            return 0
        osess.add(om.NotificationJob(
            job_key=job_key, kind=kind, scheduled_for=fire_at,
            payload_json=json.dumps(payload, ensure_ascii=False)))
        return 1


def run_due_jobs(data_dir: Path, now: datetime | None,
                 notifier: Notifier, limit: int = 50) -> dict:
    """Claim due jobs atomically and send them. Safe across restarts and
    concurrent runners: the claim (status pending->sending) wins for exactly
    one claimer (rowcount check)."""
    now = ensure_utc(now) if now else utcnow()
    stats = {"sent": 0, "failed": 0, "retried": 0, "offices": 0}
    with dbm.identity_session(data_dir) as s:
        office_ids = [o.id for o in s.query(im.Office).all()]
    for oid in office_ids:
        stats["offices"] += 1
        with dbm.office_session(data_dir, oid) as osess:
            due = (osess.query(om.NotificationJob)
                   .filter(om.NotificationJob.status == "pending",
                           om.NotificationJob.scheduled_for <= now)
                   .order_by(om.NotificationJob.scheduled_for)
                   .limit(limit).all())
            ids = [j.id for j in due]
        for jid in ids:
            _send_one(data_dir, oid, jid, now, notifier, stats)
    return stats


def _recipient_active(data_dir: Path, office_id: int, user_id: int) -> bool:
    """Anyone whose membership ended must not receive future notifications."""
    with dbm.identity_session(data_dir) as s:
        return (s.query(im.Membership)
                .filter_by(user_id=user_id, office_id=office_id,
                           status="active").first() is not None)


def _send_one(data_dir: Path, office_id: int, job_id: int, now: datetime,
              notifier: Notifier, stats: dict) -> None:
    with dbm.office_session(data_dir, office_id) as osess:
        claimed = (osess.query(om.NotificationJob)
                   .filter_by(id=job_id, status="pending")
                   .update({"status": "sending", "updated_at": now}))
        if claimed != 1:
            return  # another runner claimed it
        job = osess.get(om.NotificationJob, job_id)
        payload = json.loads(job.payload_json or "{}")
        user_id, text = payload.get("user_id"), payload.get("text", "")
    if user_id is not None and not _recipient_active(data_dir, office_id, user_id):
        with dbm.office_session(data_dir, office_id) as osess:
            job = osess.get(om.NotificationJob, job_id)
            job.status = "cancelled"
            job.last_error = "recipient no longer has an active membership"
            job.updated_at = now
        stats["cancelled"] = stats.get("cancelled", 0) + 1
        return
    # send OUTSIDE the session: network I/O must not hold a DB transaction
    try:
        ok = bool(notifier.send(user_id, text))
        err = ""
    except Exception as e:  # noqa: BLE001
        ok, err = False, str(e)[:300]
    with dbm.office_session(data_dir, office_id) as osess:
        job = osess.get(om.NotificationJob, job_id)
        job.attempts += 1
        job.updated_at = now
        if ok:
            job.status = "sent"
            job.sent_at = now
            stats["sent"] += 1
        elif job.attempts >= job.max_attempts:
            job.status = "failed"
            job.last_error = err or "notifier returned False"
            stats["failed"] += 1
        else:
            job.status = "pending"  # retry later
            job.last_error = err or "notifier returned False"
            stats["retried"] += 1
