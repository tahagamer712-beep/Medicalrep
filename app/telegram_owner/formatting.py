"""Message formatting. SECURITY: nothing here may include secrets —
no bot tokens, no encryption keys, no passwords, no absolute key paths.
"""
from datetime import datetime

from app.core.time_utils import to_office_tz

PAGE_SIZE = 10


def _ts(dt: datetime | None, tz: str = "Asia/Baghdad") -> str:
    if dt is None:
        return "—"
    return to_office_tz(dt, tz).strftime("%Y-%m-%d %H:%M")


def status_text(companies, offices, users_count, backups_info) -> str:
    lines = ["📊 *حالة النظام*", ""]
    lines.append(f"🏢 الشركات: {len(companies)}")
    lines.append(f"🏬 المكاتب: {len(offices)}")
    lines.append(f"👥 المستخدمون: {users_count}")
    lines.append("")
    lines.append("💾 *النسخ الاحتياطية:*")
    if not backups_info:
        lines.append("لا توجد نسخ بعد")
    for name, last, verified in backups_info:
        mark = "✅" if verified else "⚠️"
        lines.append(f"{mark} {name}: {last or 'لا توجد نسخة'}")
    return "\n".join(lines)


def company_text(company, offices) -> str:
    lines = [f"🏢 *{company.name_ar}*", ""]
    for o in offices:
        lines.append(f"• {o.name_ar} — {o.city} ({o.status})")
    if not offices:
        lines.append("لا توجد مكاتب")
    return "\n".join(lines)


def office_text(office, reps_count, users_count, last_backup) -> str:
    return "\n".join([
        f"🏬 *{office.name_ar}*",
        f"📍 {office.city} — {office.area}",
        f"🕐 الدوام: {office.work_start}–{office.work_end}",
        f"🩺 المندوبون: {reps_count}",
        f"👥 المستخدمون: {users_count}",
        f"💾 آخر نسخة: {last_backup or 'لا توجد'}",
    ])


def rep_text(user, rep, office_name) -> str:
    return "\n".join([
        f"🩺 *{user.full_name}*",
        f"🏬 المكتب: {office_name}",
        f"🔢 الرقم الوظيفي: {rep.employee_code}",
        f"📞 الهاتف: {user.phone}",
        f"📌 الحالة: {rep.job_status}",
        f"📅 التعيين: {rep.hire_date or '—'}",
        f"📝 ملاحظات: {rep.notes or '—'}",
    ])


def audit_text(events) -> str:
    lines = ["📝 *سجل التدقيق (الأحدث أولًا)*", ""]
    for e in events:
        lines.append(f"• `{e.event_type}` — {e.result} — {_ts(e.occurred_at)}")
        if e.reason:
            lines.append(f"  _{e.reason[:80]}_")
    if not events:
        lines.append("لا توجد أحداث")
    return "\n".join(lines)


def denied_text() -> str:
    return "⛔ غير مصرح لك باستخدام هذا البوت."


def error_text() -> str:
    return "⚠️ حدث خطأ أثناء تنفيذ الطلب. حاول لاحقًا."
