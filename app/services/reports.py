"""Reports: daily / weekly / monthly + rep performance + Excel/CSV export.

- Reports are READ models built from the office DB; Excel/CSV are export
  formats only, never a source of truth.
- Supervisor+ sees the whole office; a rep sees only their own rows.
- Dates are interpreted in the OFFICE timezone (Asia/Baghdad default).
"""
from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import Workbook

from app.core.errors import NotFound, PermissionDenied, ValidationError
from app.core.permissions import require_permission
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im


def _viewer_scope(isess, actor: int, office_id: int) -> int | None:
    """None = whole office (supervisor+); otherwise the rep's own user id."""
    try:
        require_permission(isess, actor, "report.view", office_id=office_id)
        return None
    except PermissionDenied:
        m = (isess.query(im.Membership)
             .filter_by(user_id=actor, office_id=office_id, role="rep",
                        status="active").first())
        if m is None:
            raise PermissionDenied("no report access in this office")
        return actor


def _office_ctx(data_dir: Path, office_id: int):
    with dbm.identity_session(data_dir) as s:
        o = s.get(im.Office, office_id)
        if o is None:
            raise NotFound("office not found")
        reps = [(m.user_id) for m in s.query(im.Membership)
                .filter_by(office_id=office_id, role="rep", status="active").all()]
        return (o.timezone or "Asia/Baghdad"), reps


def _local_date(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")


def _visits_by_day(osess, rep_id: int, tz: str) -> dict[str, list]:
    out: dict[str, list] = {}
    for v in osess.query(om.ActualVisit).filter_by(
            representative_id=rep_id, status="recorded").all():
        out.setdefault(_local_date(v.visited_at, tz), []).append(v)
    return out


def _attendance_map(osess, rep_id: int) -> dict[str, om.AttendanceRecord]:
    return {r.date: r for r in osess.query(om.AttendanceRecord)
            .filter_by(representative_id=rep_id).all()}


def _leave_days(osess, rep_id: int, start: str, end: str) -> int:
    total = 0
    for r in osess.query(om.LeaveRequest).filter_by(
            representative_id=rep_id, status="approved").all():
        if r.end_date >= start and r.start_date <= end:
            total += r.days_count
    return total


def _plan_status(osess, rep_id: int, week_start: str, day: str) -> dict:
    w = (osess.query(om.WeeklyPlan)
         .filter_by(representative_id=rep_id, week_start=week_start).first())
    dp = (osess.query(om.DailyPlan)
          .filter_by(representative_id=rep_id, date=day).first())
    return {"weekly": w.status if w else "none",
            "daily": dp.status if dp else "none"}


def _rep_day(osess, rep_id: int, day: str, week_start: str, tz: str,
             visits_by_day: dict, att_map: dict) -> dict:
    visits = visits_by_day.get(day, [])
    att = att_map.get(day)
    plan = _plan_status(osess, rep_id, week_start, day)
    return {
        "rep_id": rep_id, "date": day,
        "check_in": att.check_in_at.isoformat() if att and att.check_in_at else "",
        "check_out": att.check_out_at.isoformat() if att and att.check_out_at else "",
        "attendance": att.status if att else "absent",
        "late_minutes": att.late_minutes if att else 0,
        "visits": len(visits),
        "visits_done": sum(1 for v in visits if v.outcome == "done"),
        "weekly_plan": plan["weekly"], "daily_plan": plan["daily"],
    }


def daily_report(data_dir: Path, actor: int, *, office_id: int,
                 day: str) -> dict:
    date.fromisoformat(day)  # validates
    with dbm.identity_session(data_dir) as s:
        scope = _viewer_scope(s, actor, office_id)
    tz, reps = _office_ctx(data_dir, office_id)
    if scope is not None:
        reps = [scope]
    monday = (date.fromisoformat(day) -
              timedelta(days=date.fromisoformat(day).weekday())).isoformat()
    rows = []
    with dbm.office_session(data_dir, office_id) as osess:
        for rid in reps:
            rows.append(_rep_day(osess, rid, day, monday, tz,
                                 _visits_by_day(osess, rid, tz),
                                 _attendance_map(osess, rid)))
    return {"title": f"التقرير اليومي {day}", "generated_at": utcnow().isoformat(),
            "office_id": office_id, "rows": rows,
            "totals": {"reps": len(rows),
                       "visits": sum(r["visits"] for r in rows),
                       "present": sum(1 for r in rows
                                      if r["attendance"] in ("present", "late"))}}


def weekly_report(data_dir: Path, actor: int, *, office_id: int,
                  week_start: str) -> dict:
    start = date.fromisoformat(week_start)
    if start.weekday() != 0:
        raise ValidationError("week_start must be a Monday (YYYY-MM-DD)")
    days = [(start + timedelta(days=i)).isoformat() for i in range(7)]
    with dbm.identity_session(data_dir) as s:
        scope = _viewer_scope(s, actor, office_id)
    tz, reps = _office_ctx(data_dir, office_id)
    if scope is not None:
        reps = [scope]
    rows = []
    with dbm.office_session(data_dir, office_id) as osess:
        for rid in reps:
            vbd = _visits_by_day(osess, rid, tz)
            att = _attendance_map(osess, rid)
            visits = sum(len(vbd.get(d, [])) for d in days)
            present = sum(1 for d in days
                          if d in att and att[d].status in ("present", "late"))
            late = sum(1 for d in days
                       if d in att and att[d].status == "late")
            wplan = (osess.query(om.WeeklyPlan)
                     .filter_by(representative_id=rid,
                                week_start=week_start).first())
            dplans = (osess.query(om.DailyPlan)
                      .filter(om.DailyPlan.representative_id == rid,
                              om.DailyPlan.date.in_(days)).all())
            rows.append({
                "rep_id": rid, "week": week_start, "visits": visits,
                "days_present": present, "days_late": late,
                "days_absent": 7 - present,
                "leave_days": _leave_days(osess, rid, days[0], days[-1]),
                "weekly_plan": wplan.status if wplan else "none",
                "daily_plans_submitted": sum(
                    1 for p in dplans if p.status in ("submitted", "under_review",
                                                      "approved")),
            })
    return {"title": f"التقرير الأسبوعي {week_start}",
            "generated_at": utcnow().isoformat(), "office_id": office_id,
            "rows": rows,
            "totals": {"reps": len(rows),
                       "visits": sum(r["visits"] for r in rows)}}


def monthly_report(data_dir: Path, actor: int, *, office_id: int,
                   year: int, month: int) -> dict:
    if not 1 <= month <= 12:
        raise ValidationError("month must be 1-12")
    start = date(year, month, 1)
    end = (date(year + 1, 1, 1) if month == 12
           else date(year, month + 1, 1)) - timedelta(days=1)
    with dbm.identity_session(data_dir) as s:
        scope = _viewer_scope(s, actor, office_id)
    tz, reps = _office_ctx(data_dir, office_id)
    if scope is not None:
        reps = [scope]
    rows = []
    with dbm.office_session(data_dir, office_id) as osess:
        period = (osess.query(om.PayrollPeriod)
                  .filter_by(year=year, month=month).first())
        for rid in reps:
            vbd = _visits_by_day(osess, rid, tz)
            att = _attendance_map(osess, rid)
            days = []
            d = start
            while d <= end:
                days.append(d.isoformat())
                d += timedelta(days=1)
            visits = sum(len(vbd.get(x, [])) for x in days)
            present = sum(1 for x in days
                          if x in att and att[x].status in ("present", "late"))
            net = None
            if period:
                rec = (osess.query(om.SalaryRecord)
                       .filter_by(payroll_period_id=period.id,
                                  representative_id=rid).first())
                if rec:
                    t = _payroll_totals(osess, period.id, rid)
                    net = rec.base_salary + t["bonuses"] - t["deductions"]
            rows.append({
                "rep_id": rid, "month": f"{year}-{month:02d}", "visits": visits,
                "days_present": present,
                "leave_days": _leave_days(osess, rid, days[0], days[-1]),
                "net_salary": net if net is not None else "",
            })
    return {"title": f"التقرير الشهري {year}-{month:02d}",
            "generated_at": utcnow().isoformat(), "office_id": office_id,
            "rows": rows,
            "totals": {"reps": len(rows),
                       "visits": sum(r["visits"] for r in rows)}}


def rep_performance(data_dir: Path, actor: int, *, office_id: int,
                    rep_user_id: int, start_date: str, end_date: str) -> dict:
    """Manager/supervisor view of one rep across a custom range (or the rep)."""
    date.fromisoformat(start_date)
    date.fromisoformat(end_date)
    with dbm.identity_session(data_dir) as s:
        scope = _viewer_scope(s, actor, office_id)
        if scope is not None and scope != rep_user_id:
            raise PermissionDenied("can only view own performance")
    tz, _ = _office_ctx(data_dir, office_id)
    with dbm.office_session(data_dir, office_id) as osess:
        vbd = _visits_by_day(osess, rep_user_id, tz)
        att = _attendance_map(osess, rep_user_id)
        days = []
        d = date.fromisoformat(start_date)
        endd = date.fromisoformat(end_date)
        while d <= endd:
            days.append(d.isoformat())
            d += timedelta(days=1)
        visits = sum(len(vbd.get(x, [])) for x in days)
        done = sum(1 for x in days for v in vbd.get(x, [])
                   if v.outcome == "done")
        present = sum(1 for x in days
                      if x in att and att[x].status in ("present", "late"))
        late_min = sum(att[x].late_minutes for x in days if x in att)
        wplans = (osess.query(om.WeeklyPlan)
                  .filter(om.WeeklyPlan.representative_id == rep_user_id,
                          om.WeeklyPlan.week_start >= days[0],
                          om.WeeklyPlan.week_start <= days[-1]).all())
        txns = (osess.query(om.FinancialTransaction)
                .filter_by(representative_id=rep_user_id, status="applied").all())
        bonuses = sum(t.signed_amount for t in txns if t.signed_amount > 0)
        deductions = sum(-t.signed_amount for t in txns if t.signed_amount < 0)
        return {"title": f"أداء المندوب {rep_user_id}",
                "generated_at": utcnow().isoformat(), "office_id": office_id,
                "rep_id": rep_user_id, "range": f"{start_date}..{end_date}",
                "rows": [{
                    "visits": visits, "visits_done": done,
                    "days_present": present, "total_late_minutes": late_min,
                    "weekly_plans": len(wplans),
                    "plans_approved": sum(1 for p in wplans
                                          if p.status == "approved"),
                    "leave_days": _leave_days(osess, rep_user_id, days[0],
                                              days[-1]),
                    "bonuses": bonuses, "deductions": deductions,
                }],
                "totals": {}}


def _payroll_totals(osess, period_id: int, rep_id: int) -> dict:
    txns = (osess.query(om.FinancialTransaction)
            .filter_by(payroll_period_id=period_id, representative_id=rep_id,
                       status="applied").all())
    return {"bonuses": sum(t.signed_amount for t in txns if t.signed_amount > 0),
            "deductions": sum(-t.signed_amount for t in txns
                              if t.signed_amount < 0)}


# --- export -------------------------------------------------------------------

def export_csv(report: dict, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rows = report.get("rows", [])
    if not rows:
        out_path.write_text("", encoding="utf-8")
        return out_path
    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return out_path


def export_excel(report: dict, out_path: str | Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "تقرير"
    ws.append([report.get("title", ""), report.get("generated_at", "")])
    rows = report.get("rows", [])
    if rows:
        ws.append(list(rows[0].keys()))
        for r in rows:
            ws.append(list(r.values()))
    totals = report.get("totals", {})
    if totals:
        ws.append([])
        ws.append(["الإجمالي"])
        for k, v in totals.items():
            ws.append([k, v])
    wb.save(out_path)
    return out_path
