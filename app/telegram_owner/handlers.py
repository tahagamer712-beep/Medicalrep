"""Owner bot handlers. Every interaction re-authenticates the Telegram user;
hiding buttons is never the security boundary.

Phase 7: write actions are wired to the real services (plans review, rep
terminate/transfer, company/office/user creation). Every callback re-checks
permission inside the service — the button is never the authorization.
"""
from __future__ import annotations

import functools
import logging
from pathlib import Path

from telegram import Update
from telegram.ext import ContextTypes

from app.core.errors import DomainError, PermissionDenied
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.identity import services as identity_svc
from app.services import plans as plans_svc
from app.services import representatives as rep_svc
from app.telegram_owner import auth, formatting, keyboards
from app.telegram_owner.formatting import PAGE_SIZE

logger = logging.getLogger("owner_bot")


# --- auth + reply plumbing ---------------------------------------------------

async def _actor(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    tg_id = update.effective_user.id
    settings = context.bot_data["settings"]
    data_dir: Path = context.bot_data["data_dir"]
    return auth.resolve_owner_actor(settings.owner_ids, data_dir, tg_id)


async def _reply(update: Update, text: str, keyboard=None) -> None:
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text, reply_markup=keyboard, parse_mode="Markdown")
    elif update.message:
        await update.message.reply_text(
            text, reply_markup=keyboard, parse_mode="Markdown")


def _guarded(fn):
    """Authenticate + translate errors. Never leaks internals or secrets."""
    @functools.wraps(fn)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        try:
            actor = await _actor(update, context)
        except PermissionDenied:
            await _reply(update, formatting.denied_text())
            return
        except Exception:
            # DB failure (or anything else) during auth -> generic error,
            # never a traceback, never a bypass.
            logger.exception("owner bot auth failed")
            await _reply(update, formatting.error_text())
            return
        try:
            await fn(update, context, actor)
        except PermissionDenied:
            await _reply(update, formatting.denied_text())
        except DomainError as e:
            await _reply(update, f"⚠️ {e}", keyboards.back_to_main())
        except Exception:
            logger.exception("owner bot handler failed")
            await _reply(update, formatting.error_text(),
                         keyboards.back_to_main())
    return wrapper


# --- data readers (read-only, permission-checked) ----------------------------

def _status(data_dir: Path, actor: int):
    with dbm.identity_session(data_dir) as s:
        companies = s.query(im.Company).order_by(im.Company.id).all()
        offices = s.query(im.Office).order_by(im.Office.id).all()
        users_count = s.query(im.User).count()
    backups = []
    for o in offices:
        try:
            with dbm.office_session(data_dir, o.id) as osess:
                rec = (osess.query(om.BackupRecord)
                       .order_by(om.BackupRecord.id.desc()).first())
                backups.append((o.name_ar,
                                rec.taken_at.strftime("%Y-%m-%d %H:%M") if rec else None,
                                bool(rec and rec.verified)))
        except Exception:
            backups.append((o.name_ar, None, False))
    return companies, offices, users_count, backups


def _users_page(data_dir: Path, page: int):
    with dbm.identity_session(data_dir) as s:
        q = s.query(im.User).order_by(im.User.id)
        total = q.count()
        users = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
        roles = {}
        for u in users:
            ms = (s.query(im.Membership)
                  .filter_by(user_id=u.id, status="active").all())
            roles[u.id] = ",".join(sorted({m.role for m in ms})) or "—"
    return users, roles, total


def _reps_page(data_dir: Path, office_id: int, page: int):
    with dbm.office_session(data_dir, office_id) as osess:
        q = osess.query(om.Representative).order_by(om.Representative.id)
        total = q.count()
        reps = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
        rep_ids = [r.user_id for r in reps]
    with dbm.identity_session(data_dir) as s:
        names = {u.id: u.full_name for u in
                 s.query(im.User).filter(im.User.id.in_(rep_ids)).all()} if rep_ids else {}
    return reps, names, total


def _search_reps(data_dir: Path, actor: int, query: str):
    q = query.strip()
    if len(q) < 2:
        return []
    with dbm.identity_session(data_dir) as s:
        users = (s.query(im.User)
                 .filter((im.User.full_name.like(f"%{q}%")) |
                         (im.User.phone.like(f"%{q}%")))
                 .limit(20).all())
        offices = {o.id: o.name_ar for o in s.query(im.Office).all()}
        rep_offices = {}
        for u in users:
            ms = (s.query(im.Membership)
                  .filter_by(user_id=u.id, role="rep", status="active").all())
            for m in ms:
                rep_offices.setdefault(u.id, []).append(m.office_id)
    results = []
    for u in users:
        for oid in rep_offices.get(u.id, []):
            try:
                rec = rep_svc.get_representative(data_dir, actor,
                                                 office_id=oid, rep_user_id=u.id)
                results.append((u, rec, offices.get(oid, "?")))
            except DomainError:
                continue
    return results


def _audit_page(data_dir: Path, page: int):
    with dbm.identity_session(data_dir) as s:
        q = (s.query(im.CentralAuditEvent)
             .order_by(im.CentralAuditEvent.id.desc()))
        total = q.count()
        events = q.offset(page * PAGE_SIZE).limit(PAGE_SIZE).all()
    return events, total


# --- commands -----------------------------------------------------------------

@_guarded
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: int):
    await _reply(update,
                 "👋 أهلًا بك في لوحة تحكم الـ Owner.\nاختر من القائمة:",
                 keyboards.main_menu())


@_guarded
async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: int):
    await _reply(update, "📋 القائمة الرئيسية:", keyboards.main_menu())


@_guarded
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: int):
    data_dir: Path = context.bot_data["data_dir"]
    flow = context.user_data.get("flow")
    if flow:
        await _flow_text(update, context, actor, flow)
        return
    if context.user_data.get("awaiting") != "rep_search":
        await _reply(update, "استخدم /menu للتنقل.", keyboards.main_menu())
        return
    context.user_data.pop("awaiting", None)
    results = _search_reps(data_dir, actor, update.message.text or "")
    if not results:
        await _reply(update, "🔍 لا توجد نتائج.", keyboards.back_to_main())
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = [[InlineKeyboardButton(
        f"{u.full_name} ({off})",
        callback_data=f"menu:repby:{u.id}")]
        for u, rec, off in results[:10]]
    rows.append([InlineKeyboardButton("◀️ القائمة الرئيسية",
                                      callback_data="menu:main")])
    await _reply(update, f"🔍 النتائج ({len(results)}):",
                 InlineKeyboardMarkup(rows))


# --- callbacks ------------------------------------------------------------------

@_guarded
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, actor: int):
    query = update.callback_query
    data: str = query.data or ""
    data_dir: Path = context.bot_data["data_dir"]
    parts = data.split(":")

    # Phase 7 write actions (each re-checks permission inside the service).
    if data.startswith("menu:plans") or data.startswith("menu:plan:") \
            or data.startswith("act:"):
        if await handle_write_callback(update, context, actor, data_dir,
                                        parts, data):
            return

    if data == "menu:main":
        await _reply(update, "📋 القائمة الرئيسية:", keyboards.main_menu())

    elif data == "menu:status":
        companies, offices, users_count, backups = _status(data_dir, actor)
        await _reply(update,
                     formatting.status_text(companies, offices, users_count, backups),
                     keyboards.back_to_main())

    elif data == "menu:companies":
        with dbm.identity_session(data_dir) as s:
            companies = s.query(im.Company).order_by(im.Company.id).all()
        await _reply(update, "🏢 الشركات:", keyboards.companies_list(companies))

    elif parts[:2] == ["menu", "company"] and len(parts) == 3:
        cid = int(parts[2])
        with dbm.identity_session(data_dir) as s:
            company = s.get(im.Company, cid)
            if company is None:
                raise DomainError("الشركة غير موجودة")
            offices = (s.query(im.Office).filter_by(company_id=cid)
                       .order_by(im.Office.id).all())
        await _reply(update, formatting.company_text(company, offices),
                     keyboards.company_detail_keyboard(cid, offices))

    elif parts[:2] == ["menu", "office"] and len(parts) == 3:
        oid = int(parts[2])
        with dbm.identity_session(data_dir) as s:
            office = s.get(im.Office, oid)
            if office is None:
                raise DomainError("المكتب غير موجود")
            users_count = (s.query(im.Membership)
                           .filter_by(office_id=oid, status="active").count())
        with dbm.office_session(data_dir, oid) as osess:
            reps_count = osess.query(om.Representative).count()
            rec = (osess.query(om.BackupRecord)
                   .order_by(om.BackupRecord.id.desc()).first())
        await _reply(update,
                     formatting.office_text(
                         office, reps_count, users_count,
                         rec.taken_at.strftime("%Y-%m-%d %H:%M") if rec else None),
                     keyboards.office_detail(oid))

    elif parts[:2] == ["menu", "users"] and len(parts) == 3:
        page = int(parts[2])
        users, roles, total = _users_page(data_dir, page)
        lines = ["👥 *المستخدمون*", ""]
        for u in users:
            lines.append(f"• {u.full_name} — {roles[u.id]} — {u.status}")
        has_next = (page + 1) * PAGE_SIZE < total
        kb = keyboards.paginated("menu:users", page, has_next)
        # add "new user" button above the main-menu row
        from telegram import InlineKeyboardButton
        kb.inline_keyboard.insert(
            -1, [InlineKeyboardButton("➕ مستخدم جديد",
                                     callback_data="act:user_add")])
        await _reply(update, "\n".join(lines), kb)

    elif parts[:2] == ["menu", "reps"] and len(parts) == 4:
        oid, page = int(parts[2]), int(parts[3])
        reps, names, total = _reps_page(data_dir, oid, page)
        lines = ["🩺 *المندوبون*", ""]
        for r in reps:
            lines.append(f"• {names.get(r.user_id, '?')} — {r.employee_code} "
                         f"({r.job_status})")
        from telegram import InlineKeyboardButton
        extra = [[InlineKeyboardButton(f"🔎 {names.get(r.user_id, '?')}",
                                       callback_data=f"menu:rep:{oid}:{r.user_id}")]
                 for r in reps]
        has_next = (page + 1) * PAGE_SIZE < total
        await _reply(update, "\n".join(lines),
                     keyboards.paginated(f"menu:reps:{oid}", page, has_next,
                                         extra_rows=extra))

    elif parts[:2] == ["menu", "rep"] and len(parts) == 4:
        oid, uid = int(parts[2]), int(parts[3])
        rec = rep_svc.get_representative(data_dir, actor,
                                         office_id=oid, rep_user_id=uid)
        with dbm.identity_session(data_dir) as s:
            user = s.get(im.User, uid)
            office = s.get(im.Office, oid)
        # rebuild a light rep namespace for formatting
        class _R: pass
        r = _R()
        r.employee_code, r.job_status = rec["employee_code"], rec["job_status"]
        r.hire_date, r.notes = rec["hire_date"], rec["notes"]
        await _reply(update,
                     formatting.rep_text(user, r, office.name_ar if office else "?"),
                     keyboards.rep_actions_keyboard(oid, uid))

    elif parts[:2] == ["menu", "repby"] and len(parts) == 3:
        uid = int(parts[2])
        # find the rep's current office via active membership
        with dbm.identity_session(data_dir) as s:
            m = (s.query(im.Membership)
                 .filter_by(user_id=uid, role="rep", status="active").first())
            if m is None or m.office_id is None:
                raise DomainError("المندوب غير موجود")
            oid = m.office_id
        rec = rep_svc.get_representative(data_dir, actor,
                                         office_id=oid, rep_user_id=uid)
        with dbm.identity_session(data_dir) as s:
            user = s.get(im.User, uid)
            office = s.get(im.Office, oid)
        class _R: pass
        r = _R()
        r.employee_code, r.job_status = rec["employee_code"], rec["job_status"]
        r.hire_date, r.notes = rec["hire_date"], rec["notes"]
        await _reply(update,
                     formatting.rep_text(user, r, office.name_ar if office else "?"),
                     keyboards.rep_actions_keyboard(oid, uid))

    elif data == "menu:search_rep":
        context.user_data["awaiting"] = "rep_search"
        await _reply(update, "🔍 أرسل اسم المندوب أو رقم هاتفه:",
                     keyboards.back_to_main())

    elif parts[:2] == ["menu", "audit"] and len(parts) == 3:
        page = int(parts[2])
        events, total = _audit_page(data_dir, page)
        has_next = (page + 1) * PAGE_SIZE < total
        await _reply(update, formatting.audit_text(events),
                     keyboards.paginated("menu:audit", page, has_next))

    elif data == "menu:backups":
        _, offices, _, backups = _status(data_dir, actor)
        lines = ["💾 *النسخ الاحتياطية*", ""]
        for name, last, verified in backups:
            mark = "✅ موثّقة" if verified else ("⚠️ غير موثّقة" if last else "❌ لا توجد")
            lines.append(f"• {name}: {mark} — {last or ''}")
        lines += ["", "_تذكير: احتفظ بنسخة من مفاتيح التشفير خارج الجهاز._"]
        await _reply(update, "\n".join(lines), keyboards.back_to_main())

    else:
        await _reply(update, "أمر غير معروف.", keyboards.back_to_main())


# --- Phase 7: write actions ----------------------------------------------------
# Every branch below calls a real service, which re-checks permission
# server-side. Callback data only carries identifiers, never authority.
#
# Confirm pattern: text input is stored under context.user_data["pending_*"],
# the confirm button (act:*_yes) reads it. Nothing is applied before confirm.

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def _set_flow(context, kind: str, step: str, data: dict | None = None):
    context.user_data["flow"] = {"kind": kind, "step": step,
                                 "data": data or {}}


def _stash_pending(context, key: str, data: dict):
    context.user_data.pop("flow", None)
    context.user_data[key] = data


def _user_name(data_dir: Path, user_id: int) -> str:
    with dbm.identity_session(data_dir) as s:
        u = s.get(im.User, user_id)
        return u.full_name if u else "?"


def _pending_plan_counts(data_dir: Path, actor: int) -> list[tuple]:
    out = []
    with dbm.identity_session(data_dir) as s:
        offices = s.query(im.Office).order_by(im.Office.id).all()
    for o in offices:
        n = 0
        for ptype in ("weekly", "daily"):
            try:
                for st in ("submitted", "under_review"):
                    n += len(plans_svc.list_plans(
                        data_dir, actor, office_id=o.id,
                        plan_type=ptype, status=st))
            except PermissionDenied:
                pass
        out.append((o, n))
    return out


async def _show_plan_detail(update, data_dir: Path, actor: int,
                            plan_type: str, plan_id: int, office_id: int):
    plan = plans_svc.get_plan(data_dir, actor, office_id=office_id,
                              plan_type=plan_type, plan_id=plan_id)
    rep_name = _user_name(data_dir, plan.get("rep_id", 0)) \
        if "rep_id" in plan else "?"
    lines = [f"📋 *خطة {'أسبوعية' if plan_type == 'weekly' else 'يومية'}*",
             f"المندوب: {rep_name}",
             f"الفترة: {plan['period']}",
             f"الحالة: {plan['status']} — الإصدار {plan['version']}"]
    await _reply(update, "\n".join(lines),
                 keyboards.plan_review_keyboard(plan_type, plan_id))


async def _flow_text(update, context, actor: int, flow: dict):
    """Multi-step text flows (notes, reasons, names...)."""
    data_dir: Path = context.bot_data["data_dir"]
    text = (update.message.text or "").strip()
    kind, step, fdata = flow["kind"], flow["step"], flow["data"]

    if kind == "plan_return" and step == "notes":
        fdata["notes"] = text
        _stash_pending(context, "pending_plan_return", fdata)
        ptype, pid, oid = fdata["ptype"], fdata["pid"], fdata["oid"]
        await _reply(
            update,
            f"✏️ إرجاع الخطة للتعديل؟\nالملاحظات: {text or '—'}",
            keyboards.confirm_keyboard(
                f"act:plan_return_yes:{ptype}:{pid}:{oid}",
                f"menu:plan:{ptype}:{pid}:{oid}"))

    elif kind == "rep_term" and step == "reason":
        fdata["reason"] = text
        _stash_pending(context, "pending_rep_term", fdata)
        oid, uid = fdata["oid"], fdata["uid"]
        name = _user_name(data_dir, uid)
        await _reply(
            update,
            f"⛔ إنهاء ارتباط *{name}*؟\nالسبب: {text or '—'}\n"
            f"_سيحتفظ النظام بسجلاته التاريخية._",
            keyboards.confirm_keyboard(
                f"act:rep_term_yes:{oid}:{uid}", f"menu:rep:{oid}:{uid}"))

    elif kind == "rep_transfer" and step == "code":
        fdata["code"] = text
        _stash_pending(context, "pending_transfer", fdata)
        d = fdata
        name = _user_name(data_dir, d["uid"])
        await _reply(
            update,
            f"🔀 نقل *{name}* إلى المكتب {d['to_oid']}؟\n"
            f"الكود الجديد: {text}\n_سجلات المكتب السابق تُحفظ._",
            keyboards.confirm_keyboard(
                f"act:rep_transfer_yes:{d['oid']}:{d['uid']}:{d['to_oid']}",
                f"menu:rep:{d['oid']}:{d['uid']}"))

    elif kind == "company_add" and step == "name_ar":
        fdata["name_ar"] = text
        flow["step"] = "name_en"
        await _reply(update, "أرسل الاسم الإنجليزي للشركة:",
                     keyboards.back_to_main())

    elif kind == "company_add" and step == "name_en":
        fdata["name_en"] = text
        _stash_pending(context, "pending_company", fdata)
        await _reply(
            update,
            f"🏢 إنشاء شركة *{fdata['name_ar']}* ({text})؟",
            keyboards.confirm_keyboard("act:company_add_yes", "menu:companies"))

    elif kind == "office_add" and step == "name_ar":
        fdata["name_ar"] = text
        flow["step"] = "name_en"
        await _reply(update, "أرسل الاسم الإنجليزي للمكتب:",
                     keyboards.back_to_main())

    elif kind == "office_add" and step == "name_en":
        fdata["name_en"] = text
        _stash_pending(context, "pending_office", fdata)
        await _reply(
            update,
            f"🏬 إنشاء مكتب *{fdata['name_ar']}* ({text})؟",
            keyboards.confirm_keyboard(
                "act:office_add_yes", f"menu:company:{fdata['cid']}"))

    elif kind == "user_add" and step == "name":
        fdata["name"] = text
        flow["step"] = "phone"
        await _reply(update, "أرسل رقم هاتف المستخدم:",
                     keyboards.back_to_main())

    elif kind == "user_add" and step == "phone":
        fdata["phone"] = text
        flow["step"] = "office"
        with dbm.identity_session(data_dir) as s:
            offices = s.query(im.Office).order_by(im.Office.id).all()
        await _reply(update, "اختر المكتب:",
                     keyboards.offices_pick("act:user_off", offices))

    else:
        context.user_data.pop("flow", None)
        await _reply(update, "انتهت الخطوة.", keyboards.back_to_main())


async def _do_plan_approve(update, data_dir: Path, actor: int,
                           plan_type: str, plan_id: int, office_id: int):
    plan = plans_svc.get_plan(data_dir, actor, office_id=office_id,
                              plan_type=plan_type, plan_id=plan_id)
    if plan["status"] == "submitted":
        plans_svc.review_plan(data_dir, actor, office_id=office_id,
                              plan_type=plan_type, plan_id=plan_id,
                              decision="start_review")
    out = plans_svc.review_plan(data_dir, actor, office_id=office_id,
                                plan_type=plan_type, plan_id=plan_id,
                                decision="approve", notes="اعتماد عبر البوت")
    await _reply(update, f"✅ تم اعتماد الخطة (الإصدار {out['status']}).",
                 keyboards.back_to_main())


async def _do_plan_return(update, context, data_dir: Path, actor: int,
                          plan_type: str, plan_id: int, office_id: int):
    pending = context.user_data.pop("pending_plan_return", None) or {}
    notes = pending.get("notes", "")
    plan = plans_svc.get_plan(data_dir, actor, office_id=office_id,
                              plan_type=plan_type, plan_id=plan_id)
    if plan["status"] == "submitted":
        plans_svc.review_plan(data_dir, actor, office_id=office_id,
                              plan_type=plan_type, plan_id=plan_id,
                              decision="start_review")
    out = plans_svc.review_plan(data_dir, actor, office_id=office_id,
                                plan_type=plan_type, plan_id=plan_id,
                                decision="request_changes", notes=notes)
    await _reply(update, f"✏️ أُرجعت الخطة للتعديل ({out['status']}).",
                 keyboards.back_to_main())


async def handle_write_callback(update, context, actor: int,
                                data_dir: Path, parts: list[str],
                                data: str) -> bool:
    """Handle act: callbacks. Returns True if handled."""
    query = update.callback_query

    # ---- plans review queue ----
    if data == "menu:plans":
        rows = []
        for o, n in _pending_plan_counts(data_dir, actor):
            mark = f" ({n} معلقة)" if n else ""
            rows.append([InlineKeyboardButton(
                f"🏬 {o.name_ar}{mark}",
                callback_data=f"menu:plans_off:{o.id}")])
        rows.append([InlineKeyboardButton(
            "◀️ القائمة الرئيسية", callback_data="menu:main")])
        await _reply(update, "📋 *الخطط المعلقة* — اختر المكتب:",
                     InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "plans_off"] and len(parts) == 3:
        oid = int(parts[2])
        rows = []
        for ptype, label in (("weekly", "أسبوعية"), ("daily", "يومية")):
            for st in ("submitted", "under_review"):
                for p in plans_svc.list_plans(data_dir, actor, office_id=oid,
                                              plan_type=ptype, status=st):
                    name = _user_name(data_dir, p["rep_id"])
                    rows.append([InlineKeyboardButton(
                        f"{label} — {name} — {p['period']} ({p['status']})",
                        callback_data=f"menu:plan:{ptype}:{p['plan_id']}:{oid}")])
        if not rows:
            rows.append([InlineKeyboardButton("لا توجد خطط معلقة",
                                              callback_data="menu:plans")])
        rows.append([InlineKeyboardButton("◀️ الخطط المعلقة",
                                          callback_data="menu:plans")])
        await _reply(update, "📋 الخطط المعلقة:", InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "plan"] and len(parts) == 5:
        ptype, pid, oid = parts[2], int(parts[3]), int(parts[4])
        await _show_plan_detail(update, data_dir, actor, ptype, pid, oid)
        return True

    if parts[:2] == ["act", "plan_approve"] and len(parts) == 5:
        ptype, pid, oid = parts[2], int(parts[3]), int(parts[4])
        await _reply(update, "✅ اعتماد هذه الخطة؟",
                     keyboards.confirm_keyboard(
                         f"act:plan_approve_yes:{ptype}:{pid}:{oid}",
                         f"menu:plan:{ptype}:{pid}:{oid}"))
        return True

    if parts[:2] == ["act", "plan_approve_yes"] and len(parts) == 5:
        await _do_plan_approve(update, data_dir, actor,
                               parts[2], int(parts[3]), int(parts[4]))
        return True

    if parts[:2] == ["act", "plan_return"] and len(parts) == 5:
        ptype, pid, oid = parts[2], int(parts[3]), int(parts[4])
        _set_flow(context, "plan_return", "notes",
                   {"ptype": ptype, "pid": pid, "oid": oid})
        await _reply(update, "✏️ أرسل ملاحظات التعديل المطلوبة:",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "plan_return_yes"] and len(parts) == 5:
        await _do_plan_return(update, context, data_dir, actor,
                              parts[2], int(parts[3]), int(parts[4]))
        return True

    # ---- rep actions ----
    if parts[:2] == ["act", "rep_term"] and len(parts) == 4:
        oid, uid = int(parts[2]), int(parts[3])
        _set_flow(context, "rep_term", "reason", {"oid": oid, "uid": uid})
        await _reply(update, "⛔ أرسل سبب إنهاء الارتباط:",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "rep_term_yes"] and len(parts) == 4:
        oid, uid = int(parts[2]), int(parts[3])
        pending = context.user_data.pop("pending_rep_term", None) or {}
        out = rep_svc.terminate_association(
            data_dir, actor, rep_user_id=uid, office_id=oid,
            reason=pending.get("reason", "عبر البوت"))
        await _reply(update, "⛔ تم إنهاء الارتباط.",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "rep_transfer"] and len(parts) == 4:
        oid, uid = int(parts[2]), int(parts[3])
        with dbm.identity_session(data_dir) as s:
            offices = s.query(im.Office).order_by(im.Office.id).all()
        await _reply(update, "🔀 اختر المكتب الجديد:",
                     keyboards.offices_pick(f"act:rep_transfer_off:{oid}:{uid}",
                                            offices, exclude_id=oid))
        return True

    if parts[:3] == ["act", "rep_transfer_off"] and len(parts) == 5:
        oid, uid, to_oid = int(parts[2]), int(parts[3]), int(parts[4])
        _set_flow(context, "rep_transfer", "code",
                   {"oid": oid, "uid": uid, "to_oid": to_oid})
        await _reply(update, "أرسل الكود الوظيفي الجديد في المكتب الجديد:",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "rep_transfer_yes"] and len(parts) == 5:
        oid, uid, to_oid = int(parts[2]), int(parts[3]), int(parts[4])
        pending = context.user_data.pop("pending_transfer", None) or {}
        code = pending.get("code", "")
        rep_svc.transfer_representative(
            data_dir, actor, rep_user_id=uid, from_office_id=oid,
            to_office_id=to_oid, new_employee_code=code, reason="عبر البوت")
        await _reply(update, "🔀 تم النقل بنجاح.", keyboards.back_to_main())
        return True

    # ---- company / office / user ----
    if data == "act:company_add":
        _set_flow(context, "company_add", "name_ar")
        await _reply(update, "🏢 أرسل الاسم العربي للشركة:",
                     keyboards.back_to_main())
        return True

    if data == "act:company_add_yes":
        pending = context.user_data.pop("pending_company", None) or {}
        with dbm.identity_session(data_dir) as s:
            c = identity_svc.create_company(
                s, actor, name_ar=pending["name_ar"],
                name_en=pending.get("name_en") or pending["name_ar"])
        await _reply(update, f"🏢 تم إنشاء الشركة: {c.name_ar}",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "office_add"] and len(parts) == 3:
        cid = int(parts[2])
        _set_flow(context, "office_add", "name_ar", {"cid": cid})
        await _reply(update, "🏬 أرسل الاسم العربي للمكتب:",
                     keyboards.back_to_main())
        return True

    if data == "act:office_add_yes":
        pending = context.user_data.pop("pending_office", None) or {}
        with dbm.identity_session(data_dir) as s:
            o = identity_svc.create_office(
                s, data_dir, actor, company_id=pending["cid"],
                name_ar=pending["name_ar"],
                name_en=pending.get("name_en") or pending["name_ar"])
        await _reply(update, f"🏬 تم إنشاء المكتب: {o.name_ar}",
                     keyboards.back_to_main())
        return True

    if data == "act:user_add":
        _set_flow(context, "user_add", "name")
        await _reply(update, "👤 أرسل اسم المستخدم الكامل:",
                     keyboards.back_to_main())
        return True

    if parts[:3] == ["act", "user_off"] and len(parts) == 4:
        flow = context.user_data.get("flow") or {}
        (flow.get("data") or {})["oid"] = int(parts[3])
        flow["step"] = "role"
        await _reply(update, "اختر الدور:",
                     keyboards.roles_pick("act:user_role"))
        return True

    if parts[:3] == ["act", "user_role"] and len(parts) == 4:
        flow = context.user_data.get("flow") or {}
        fdata = flow.get("data") or {}
        fdata["role"] = parts[3]
        context.user_data.pop("flow", None)
        context.user_data["pending_user"] = fdata
        await _reply(
            update,
            f"👤 إنشاء مستخدم *{fdata.get('name')}* ({fdata.get('phone')}) "
            f"بدور {fdata['role']}؟",
            keyboards.confirm_keyboard("act:user_add_yes", "menu:users:0"))
        return True

    if data == "act:user_add_yes":
        pending = context.user_data.pop("pending_user", None) or {}
        with dbm.identity_session(data_dir) as s:
            u = identity_svc.create_user(
                s, actor, full_name=pending["name"], phone=pending["phone"])
            identity_svc.assign_membership(
                s, actor, user_id=u.id, company_id=None,
                office_id=pending["oid"], role=pending["role"])
        await _reply(update, f"👤 تم إنشاء المستخدم: {u.full_name}",
                     keyboards.back_to_main())
        return True

    return False
