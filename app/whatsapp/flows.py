"""WhatsApp conversation flows.

Phone -> User resolution uses the existing User.phone column. Role gate:
rep / supervisor / manager only — owner is rejected (owner uses Telegram).

Flows reuse the existing services (plans / attendance / leave) and never
re-implement their business rules. This module only translates WhatsApp
text commands into service calls and formats the replies.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from app.core.errors import (AlreadyExists, NotFound, PermissionDenied,
                             ValidationError)
from app.core.time_utils import utcnow
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.services import attendance as attendance_svc
from app.services import leave as leave_svc
from app.whatsapp import dedupe
from app.whatsapp.sender import WhatsAppSender
from app.whatsapp.templates import send_smart

logger = logging.getLogger("whatsapp_flows")

ALLOWED_ROLES = {"rep", "supervisor", "manager"}

HELP_AR = (
    "الأوامر المتاحة:\n"
    "• الخطة — عرض خطتك اليومية\n"
    "• حضور — تسجيل الحضور\n"
    "• انصراف — تسجيل الانصراف\n"
    "• إجازة — عرض أنواع الإجازات\n"
    "• إجازة <نوع> <من> <إلى> — طلب إجازة (مثال: إجازة 1 2026-10-20 2026-10-22)"
)


def normalize_phone(phone: str) -> str:
    """Strip spaces/dashes and a leading '+'. Matching is exact after this.

    Iraqi numbers: Meta sends the international form without '+' (e.g.
    9647000000011) while User.phone is stored locally (07000000011);
    a leading 964 country code is converted to the local 0 form.
    """
    p = phone.strip().replace(" ", "").replace("-", "").lstrip("+")
    if p.startswith("964") and len(p) == 13:
        p = "0" + p[3:]
    return p


@dataclass
class WaContext:
    user_id: int
    office_id: int
    office_name: str
    role: str
    wa_phone: str


def resolve_context(data_dir: Path, wa_phone: str) -> WaContext | None:
    """Map a WhatsApp number to (user, primary office, role).

    Returns None when the number is not registered. Picks the first active
    office-level membership; users with several offices use the first one
    for WhatsApp (documented limitation).
    """
    phone = normalize_phone(wa_phone)
    with dbm.identity_session(data_dir) as s:
        user = s.query(im.User).filter_by(phone=phone).first()
        if user is None or user.status != "active":
            return None
        memberships = (s.query(im.Membership)
                       .filter_by(user_id=user.id, status="active")
                       .filter(im.Membership.office_id.isnot(None))
                       .order_by(im.Membership.id).all())
        if not memberships:
            return None
        m = memberships[0]
        office = s.query(im.Office).filter_by(id=m.office_id).one()
        return WaContext(user_id=user.id, office_id=office.id,
                         office_name=office.name_ar, role=m.role,
                         wa_phone=phone)


def _friendly_error(e: Exception) -> str:
    if isinstance(e, (PermissionDenied, ValidationError, AlreadyExists,
                      NotFound)):
        return f"تعذر التنفيذ: {e}"
    logger.exception("whatsapp flow unexpected error")
    return "حدث خطأ غير متوقع، حاول لاحقًا."


def _reply(data_dir: Path, sender: WhatsAppSender, ctx: WaContext, text: str,
           template: str, template_params: list[str],
           now: datetime) -> tuple[str, str]:
    last = dedupe.last_inbound_at(data_dir, ctx.user_id)
    return send_smart(sender, ctx.wa_phone, text, template=template,
                      template_params=template_params,
                      last_inbound_at=last, now=now)


def _cmd_daily_plan(data_dir: Path, ctx: WaContext, now: datetime) -> str:
    with dbm.identity_session(data_dir) as s:
        office = s.query(im.Office).filter_by(id=ctx.office_id).one()
        tz = office.timezone or "Asia/Baghdad"
    today = now.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
    with dbm.office_session(data_dir, ctx.office_id) as osess:
        plan = (osess.query(om.DailyPlan)
                .filter_by(representative_id=ctx.user_id, date=today)
                .first())
        if plan is None:
            return f"لا توجد خطة يومية بتاريخ {today}."
        visits = (osess.query(om.PlannedVisit)
                  .filter_by(plan_type="daily", plan_id=plan.id)
                  .order_by(om.PlannedVisit.expected_order).all())
    lines = [f"خطتك اليومية ({today}) — الحالة: {plan.status}"]
    for v in visits:
        lines.append(f"• {v.target_type} #{v.target_id}"
                     + (f" الساعة {v.expected_time}" if v.expected_time else ""))
    if not visits:
        lines.append("(لا زيارات مخططة)")
    return "\n".join(lines)


def _cmd_leave_types(data_dir: Path, ctx: WaContext) -> str:
    with dbm.office_session(data_dir, ctx.office_id) as osess:
        types = (osess.query(om.LeaveType)
                 .filter_by(status="active").order_by(om.LeaveType.id).all())
    if not types:
        return "لا توجد أنواع إجازات مفعّلة."
    lines = ["أنواع الإجازات:",
             "للطلب أرسل: إجازة <نوع> <من YYYY-MM-DD> <إلى YYYY-MM-DD>"]
    for t in types:
        lines.append(f"• {t.id}: {t.name} ({t.default_days_per_year} يوم/سنة)")
    return "\n".join(lines)


def handle_text(data_dir: Path, sender: WhatsAppSender, wa_phone: str,
                text: str, waba_message_id: str,
                now: datetime | None = None) -> dict:
    """Process one inbound text message. Returns a result dict.

    Steps: dedupe-claim -> resolve user -> role gate -> dispatch.
    Unknown numbers and disallowed roles get a reply, never a crash.
    """
    now = now or utcnow()
    phone = normalize_phone(wa_phone)
    # claim first (even for unknown users: prevents redelivery loops)
    if not dedupe.claim_message(data_dir, waba_message_id, None, phone, now):
        return {"status": "duplicate", "waba_message_id": waba_message_id}
    ctx = resolve_context(data_dir, phone)
    if ctx is None:
        sender.send_text(phone, "هذا الرقم غير مسجّل في النظام.")
        return {"status": "unknown_user", "waba_message_id": waba_message_id}
    # bind the claimed message to the resolved user for window tracking
    with dbm.identity_session(data_dir) as s:
        row = (s.query(im.WabaProcessedMessage)
               .filter_by(waba_message_id=waba_message_id).one())
        row.user_id = ctx.user_id
    if ctx.role not in ALLOWED_ROLES:
        sender.send_text(phone, "هذه القناة للمندوبين والمشرفين والمدراء.")
        return {"status": "role_rejected", "role": ctx.role}

    body = text.strip()
    low = body.lower()
    try:
        if body.startswith("الخطة") or low.startswith("plan"):
            reply = _cmd_daily_plan(data_dir, ctx, now)
            tpl, params = "daily_plan", [body, reply]
        elif body.startswith("حضور") or low.startswith("checkin"):
            r = attendance_svc.check_in(data_dir, ctx.user_id,
                                        office_id=ctx.office_id,
                                        rep_user_id=ctx.user_id, now=now)
            reply = f"تم تسجيل الحضور الساعة {r.get('check_in', '')}."
            tpl, params = "attendance_confirm", ["حضور", reply]
        elif body.startswith("انصراف") or low.startswith("checkout"):
            r = attendance_svc.check_out(data_dir, ctx.user_id,
                                         office_id=ctx.office_id,
                                         rep_user_id=ctx.user_id, now=now)
            reply = f"تم تسجيل الانصراف الساعة {r.get('check_out', '')}."
            tpl, params = "attendance_confirm", ["انصراف", reply]
        elif body.startswith("إجازة") or low.startswith("leave"):
            parts = body.split()
            if len(parts) == 4:
                _, ltype, start, end = parts
                r = leave_svc.request_leave(
                    data_dir, ctx.user_id, office_id=ctx.office_id,
                    rep_user_id=ctx.user_id, leave_type_id=int(ltype),
                    start_date=start, end_date=end)
                reply = (f"تم استلام طلب الإجازة رقم {r['request_id']} "
                         f"({r['days']} أيام) — الحالة: {r['status']}.")
                tpl, params = "leave_received", [str(r["request_id"]),
                                                 str(r["days"])]
            else:
                reply = _cmd_leave_types(data_dir, ctx)
                tpl, params = "leave_received", ["0", "0"]
        else:
            reply, tpl, params = HELP_AR, "daily_plan", ["", ""]
        kind, mid = _reply(data_dir, sender, ctx, reply, tpl, params, now)
        return {"status": "ok", "kind": kind, "provider_message_id": mid,
                "user_id": ctx.user_id}
    except Exception as e:  # noqa: BLE001 - webhook must never crash
        sender.send_text(phone, _friendly_error(e))
        return {"status": "error", "error": str(e)}
