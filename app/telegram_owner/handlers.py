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
from app.services import leave as leave_svc
from app.services import payroll as payroll_svc
from app.services import reports as reports_svc
from app.services import products as products_svc
from app.services import surveys as surveys_svc
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


async def _reply_document(update: Update, path: Path, filename: str) -> None:
    """Send a generated file (e.g. Excel export) as a Telegram document."""
    if update.callback_query:
        await update.callback_query.answer()
        msg = update.callback_query.message
        if msg is not None:
            with open(path, "rb") as f:
                await msg.reply_document(document=f, filename=filename)
            return
    if update.message:
        with open(path, "rb") as f:
            await update.message.reply_document(document=f, filename=filename)


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

    # Write menus + write actions (each re-checks permission in the service).
    _write_menu_prefixes = (
        "menu:plans", "menu:plan:",
        "menu:leaves", "menu:leave:",
        "menu:finance", "menu:deduct", "menu:salary",
        "menu:reports", "menu:report",
        "menu:products", "menu:product",
        "menu:surveys", "menu:survey:", "menu:survey_results",
    )
    if data.startswith("act:") or data.startswith(_write_menu_prefixes):
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
                 keyboards.plan_review_keyboard(plan_type, plan_id, office_id))


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

    elif kind == "leave_reject" and step == "notes":
        fdata["notes"] = text
        _stash_pending(context, "pending_leave_reject", fdata)
        oid, rid = fdata["oid"], fdata["rid"]
        await _reply(
            update,
            f"❌ رفض طلب الإجازة؟\nالسبب: {text or '—'}",
            keyboards.confirm_keyboard(
                f"act:leave_reject_yes:{oid}:{rid}",
                f"menu:leave:{oid}:{rid}"))

    elif kind == "deduct_reject" and step == "notes":
        fdata["notes"] = text
        _stash_pending(context, "pending_deduct_reject", fdata)
        oid, uuid = fdata["oid"], fdata["uuid"]
        await _reply(
            update,
            f"❌ رفض الخصم المقترح؟\nالسبب: {text or '—'}",
            keyboards.confirm_keyboard(
                f"act:deduct_reject_yes:{oid}:{uuid}",
                f"menu:deduct:{oid}:{uuid}"))

    elif kind == "product_add" and step == "trade_name":
        fdata["trade_name"] = text
        flow["step"] = "scientific"
        await _reply(update, "أرسل الاسم العلمي (أو — للتخطي):",
                     keyboards.back_to_main())

    elif kind == "product_add" and step == "scientific":
        fdata["scientific_name"] = "" if text == "—" else text
        _stash_pending(context, "pending_product", fdata)
        await _reply(
            update,
            f"💊 إنشاء منتج *{fdata['trade_name']}*؟",
            keyboards.confirm_keyboard(
                f"act:product_add_yes:{fdata['oid']}",
                f"menu:products_off:{fdata['oid']}"))

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

    # ---- leaves ----
    if data == "menu:leaves":
        rows = []
        with dbm.identity_session(data_dir) as s:
            offices = s.query(im.Office).order_by(im.Office.id).all()
        for o in offices:
            try:
                reqs = leave_svc.list_leave_requests(
                    data_dir, actor, office_id=o.id, status="pending")
            except (PermissionDenied, DomainError):
                continue
            mark = f" ({len(reqs)} معلقة)" if reqs else ""
            rows.append([InlineKeyboardButton(
                f"🏬 {o.name_ar}{mark}",
                callback_data=f"menu:leaves_off:{o.id}")])
        rows.append([InlineKeyboardButton("◀️ القائمة الرئيسية",
                                          callback_data="menu:main")])
        await _reply(update, "🌴 *طلبات الإجازة المعلقة* — اختر المكتب:",
                     InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "leaves_off"] and len(parts) == 3:
        oid = int(parts[2])
        reqs = leave_svc.list_leave_requests(
            data_dir, actor, office_id=oid, status="pending")
        rows = []
        for r in reqs:
            name = _user_name(data_dir, r["rep"])
            rows.append([InlineKeyboardButton(
                f"{name} — {r['period']} ({r['days']} أيام)",
                callback_data=f"menu:leave:{oid}:{r['request_id']}")])
        if not rows:
            rows.append([InlineKeyboardButton("لا توجد طلبات معلقة",
                                              callback_data="menu:leaves")])
        rows.append([InlineKeyboardButton("◀️ طلبات الإجازة",
                                          callback_data="menu:leaves")])
        await _reply(update, "🌴 الطلبات المعلقة:", InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "leave"] and len(parts) == 4:
        oid, rid = int(parts[2]), int(parts[3])
        with dbm.office_session(data_dir, oid) as osess:
            req = osess.get(om.LeaveRequest, rid)
            if req is None:
                raise DomainError("طلب الإجازة غير موجود")
            ltype = osess.get(om.LeaveType, req.leave_type_id)
        name = _user_name(data_dir, req.representative_id)
        lines = ["🌴 *طلب إجازة*",
                 f"المندوب: {name}",
                 f"النوع: {ltype.name if ltype else '—'}",
                 f"الفترة: {req.start_date} ← {req.end_date} ({req.days_count} أيام)",
                 f"السبب: {req.reason or '—'}",
                 f"الحالة: {req.status}"]
        kb = (keyboards.leave_review_keyboard(oid, rid)
              if req.status == "pending" else keyboards.back_to_main())
        await _reply(update, "\n".join(lines), kb)
        return True

    if parts[:2] == ["act", "leave_approve"] and len(parts) == 4:
        oid, rid = int(parts[2]), int(parts[3])
        await _reply(update, "✅ اعتماد طلب الإجازة؟",
                     keyboards.confirm_keyboard(
                         f"act:leave_approve_yes:{oid}:{rid}",
                         f"menu:leave:{oid}:{rid}"))
        return True

    if parts[:2] == ["act", "leave_approve_yes"] and len(parts) == 4:
        oid, rid = int(parts[2]), int(parts[3])
        out = leave_svc.review_leave(data_dir, actor, office_id=oid,
                                     request_id=rid, decision="approve",
                                     notes="اعتماد عبر البوت")
        await _reply(update, f"✅ تم اعتماد الإجازة ({out['status']}).",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "leave_reject"] and len(parts) == 4:
        oid, rid = int(parts[2]), int(parts[3])
        _set_flow(context, "leave_reject", "notes", {"oid": oid, "rid": rid})
        await _reply(update, "❌ أرسل سبب الرفض:",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "leave_reject_yes"] and len(parts) == 4:
        oid, rid = int(parts[2]), int(parts[3])
        pending = context.user_data.pop("pending_leave_reject", None) or {}
        out = leave_svc.review_leave(data_dir, actor, office_id=oid,
                                     request_id=rid, decision="reject",
                                     notes=pending.get("notes", ""))
        await _reply(update, f"❌ تم رفض طلب الإجازة ({out['status']}).",
                     keyboards.back_to_main())
        return True

    # ---- finance ----
    if data == "menu:finance":
        await _reply(update, "💰 *المالية* — اختر:",
                     InlineKeyboardMarkup([
                         [InlineKeyboardButton(
                             "📋 الخصومات المقترحة",
                             callback_data="menu:deducts")],
                         [InlineKeyboardButton(
                             "🧾 ملخص راتب مندوب",
                             callback_data="menu:salary")],
                         [InlineKeyboardButton(
                             "◀️ القائمة الرئيسية",
                             callback_data="menu:main")],
                     ]))
        return True

    if data == "menu:deducts":
        with dbm.identity_session(data_dir) as s:
            offices = s.query(im.Office).order_by(im.Office.id).all()
        await _reply(update, "📋 اختر المكتب:",
                     keyboards.offices_pick("menu:deducts_off", offices))
        return True

    if parts[:2] == ["menu", "deducts_off"] and len(parts) == 3:
        oid = int(parts[2])
        txns = payroll_svc.list_transactions(
            data_dir, actor, office_id=oid, status="pending")
        rows = []
        for t in [x for x in txns if x["kind"] == "deduction_proposed"]:
            name = _user_name(data_dir, t["rep"])
            rows.append([InlineKeyboardButton(
                f"{name} — {abs(t['amount']):,} د.ع. — {t['reason'][:20]}",
                callback_data=f"menu:deduct:{oid}:{t['uuid']}")])
        if not rows:
            rows.append([InlineKeyboardButton("لا توجد خصومات معلقة",
                                              callback_data="menu:deducts")])
        rows.append([InlineKeyboardButton("◀️ المالية",
                                          callback_data="menu:finance")])
        await _reply(update, "📋 الخصومات المقترحة المعلقة:",
                     InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "deduct"] and len(parts) == 4:
        oid, uuid = int(parts[2]), parts[3]
        txns = payroll_svc.list_transactions(data_dir, actor, office_id=oid)
        t = next((x for x in txns if x["uuid"] == uuid), None)
        if t is None:
            raise DomainError("الخصم غير موجود")
        name = _user_name(data_dir, t["rep"])
        lines = ["📋 *خصم مقترح*",
                 f"المندوب: {name}",
                 f"المبلغ: {abs(t['amount']):,} د.ع.",
                 f"السبب: {t['reason'] or '—'}",
                 f"الحالة: {t['status']}"]
        kb = (keyboards.deduct_review_keyboard(oid, uuid)
              if t["status"] == "pending" else keyboards.back_to_main())
        await _reply(update, "\n".join(lines), kb)
        return True

    if parts[:2] == ["act", "deduct_approve"] and len(parts) == 4:
        oid, uuid = int(parts[2]), parts[3]
        await _reply(update, "✅ اعتماد هذا الخصم؟ _سيُطبق فورًا._",
                     keyboards.confirm_keyboard(
                         f"act:deduct_approve_yes:{oid}:{uuid}",
                         f"menu:deduct:{oid}:{uuid}"))
        return True

    if parts[:2] == ["act", "deduct_approve_yes"] and len(parts) == 4:
        oid, uuid = int(parts[2]), parts[3]
        out = payroll_svc.review_proposed_deduction(
            data_dir, actor, office_id=oid, txn_uuid=uuid,
            decision="approve", notes="اعتماد عبر البوت")
        await _reply(update, f"✅ تم اعتماد الخصم ({out['status']}).",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "deduct_reject"] and len(parts) == 4:
        oid, uuid = int(parts[2]), parts[3]
        _set_flow(context, "deduct_reject", "notes",
                   {"oid": oid, "uuid": uuid})
        await _reply(update, "❌ أرسل سبب الرفض:",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "deduct_reject_yes"] and len(parts) == 4:
        oid, uuid = int(parts[2]), parts[3]
        pending = context.user_data.pop("pending_deduct_reject", None) or {}
        out = payroll_svc.review_proposed_deduction(
            data_dir, actor, office_id=oid, txn_uuid=uuid,
            decision="reject", notes=pending.get("notes", ""))
        await _reply(update, f"❌ تم رفض الخصم ({out['status']}).",
                     keyboards.back_to_main())
        return True

    if data == "menu:salary":
        with dbm.identity_session(data_dir) as s:
            offices = s.query(im.Office).order_by(im.Office.id).all()
        await _reply(update, "🧾 اختر المكتب:",
                     keyboards.offices_pick("menu:salary_off", offices))
        return True

    if parts[:2] == ["menu", "salary_off"] and len(parts) == 3:
        oid = int(parts[2])
        reps, names, _ = _reps_page(data_dir, oid, 0)
        rows = [[InlineKeyboardButton(
            f"🩺 {names.get(r.user_id, '?')} — {r.employee_code}",
            callback_data=f"menu:salary_rep:{oid}:{r.user_id}")]
            for r in reps]
        if not rows:
            rows.append([InlineKeyboardButton("لا يوجد مندوبون",
                                              callback_data="menu:salary")])
        rows.append([InlineKeyboardButton("◀️ المالية",
                                          callback_data="menu:finance")])
        await _reply(update, "🧾 اختر المندوب:", InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "salary_rep"] and len(parts) == 4:
        oid, uid = int(parts[2]), int(parts[3])
        with dbm.office_session(data_dir, oid) as osess:
            periods = (osess.query(om.PayrollPeriod)
                       .order_by(om.PayrollPeriod.id.desc()).limit(12).all())
        rows = [[InlineKeyboardButton(
            f"🗓️ {p.name} ({p.status})",
            callback_data=f"menu:salary_view:{oid}:{uid}:{p.id}")]
            for p in periods]
        if not rows:
            rows.append([InlineKeyboardButton("لا توجد فترات",
                                              callback_data=f"menu:salary_off:{oid}")])
        rows.append([InlineKeyboardButton("◀️ المندوبون",
                                          callback_data=f"menu:salary_off:{oid}")])
        await _reply(update, "🗓️ اختر الفترة:", InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "salary_view"] and len(parts) == 5:
        oid, uid, pid = int(parts[2]), int(parts[3]), int(parts[4])
        summ = payroll_svc.salary_summary(data_dir, actor, office_id=oid,
                                          rep_user_id=uid, period_id=pid)
        name = _user_name(data_dir, uid)
        lines = [f"🧾 *ملخص الراتب — {name}*",
                 f"الفترة: {summ['period']} ({summ['status']})",
                 f"الراتب الأساسي: {summ['base_salary']:,} د.ع.",
                 f"المكافآت: {summ['bonuses']:,} د.ع.",
                 f"الاستقطاعات: {summ['deductions']:,} د.ع.",
                 f"*الصافي: {summ['net']:,} د.ع.*"]
        await _reply(update, "\n".join(lines), keyboards.back_to_main())
        return True

    # ---- reports ----
    if data == "menu:reports":
        with dbm.identity_session(data_dir) as s:
            offices = s.query(im.Office).order_by(im.Office.id).all()
        await _reply(update, "📈 اختر المكتب:",
                     keyboards.offices_pick("menu:reports_off", offices))
        return True

    if parts[:2] == ["menu", "reports_off"] and len(parts) == 3:
        oid = int(parts[2])
        await _reply(update, "📈 *التقارير* — اختر:",
                     InlineKeyboardMarkup([
                         [InlineKeyboardButton(
                             "📅 التقرير اليومي (اليوم)",
                             callback_data=f"menu:report_daily:{oid}")],
                         [InlineKeyboardButton(
                             "🗓️ التقرير الأسبوعي (هذا الأسبوع)",
                             callback_data=f"menu:report_weekly:{oid}")],
                         [InlineKeyboardButton(
                             "🏅 أداء مندوب (آخر 30 يومًا)",
                             callback_data=f"menu:report_perf_pick:{oid}")],
                         [InlineKeyboardButton(
                             "◀️ التقارير", callback_data="menu:reports")],
                     ]))
        return True

    if parts[:2] == ["menu", "report_daily"] and len(parts) == 3:
        oid = int(parts[2])
        from datetime import date
        day = date.today().isoformat()
        rep = reports_svc.daily_report(data_dir, actor, office_id=oid, day=day)
        t = rep["totals"]
        lines = [f"📅 *{rep['title']}*",
                 f"المندوبون: {t['reps']}",
                 f"الزيارات: {t['visits']}",
                 f"الحاضرون: {t['present']}"]
        rows = [[InlineKeyboardButton(
            "⬇️ تنزيل Excel",
            callback_data=f"act:report_xlsx:daily:{oid}:{day}")],
            [InlineKeyboardButton("◀️ التقارير",
                                  callback_data=f"menu:reports_off:{oid}")]]
        await _reply(update, "\n".join(lines), InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "report_weekly"] and len(parts) == 3:
        oid = int(parts[2])
        from datetime import date, timedelta
        monday = (date.today() -
                  timedelta(days=date.today().weekday())).isoformat()
        rep = reports_svc.weekly_report(data_dir, actor, office_id=oid,
                                        week_start=monday)
        t = rep["totals"]
        lines = [f"🗓️ *{rep['title']}*",
                 f"المندوبون: {t['reps']}",
                 f"الزيارات: {t['visits']}"]
        rows = [[InlineKeyboardButton(
            "⬇️ تنزيل Excel",
            callback_data=f"act:report_xlsx:weekly:{oid}:{monday}")],
            [InlineKeyboardButton("◀️ التقارير",
                                  callback_data=f"menu:reports_off:{oid}")]]
        await _reply(update, "\n".join(lines), InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "report_perf_pick"] and len(parts) == 3:
        oid = int(parts[2])
        reps, names, _ = _reps_page(data_dir, oid, 0)
        rows = [[InlineKeyboardButton(
            f"🩺 {names.get(r.user_id, '?')} — {r.employee_code}",
            callback_data=f"menu:report_perf:{oid}:{r.user_id}")]
            for r in reps]
        if not rows:
            rows.append([InlineKeyboardButton(
                "لا يوجد مندوبون", callback_data=f"menu:reports_off:{oid}")])
        rows.append([InlineKeyboardButton(
            "◀️ التقارير", callback_data=f"menu:reports_off:{oid}")])
        await _reply(update, "🏅 اختر المندوب:", InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "report_perf"] and len(parts) == 4:
        oid, uid = int(parts[2]), int(parts[3])
        from datetime import date, timedelta
        end = date.today().isoformat()
        start = (date.today() - timedelta(days=30)).isoformat()
        rep = reports_svc.rep_performance(data_dir, actor, office_id=oid,
                                          rep_user_id=uid,
                                          start_date=start, end_date=end)
        row = rep["rows"][0]
        name = _user_name(data_dir, uid)
        lines = [f"🏅 *أداء {name}* ({start} ← {end})",
                 f"الزيارات: {row['visits']} (منجزة: {row['visits_done']})",
                 f"أيام الحضور: {row['days_present']}",
                 f"دقائق التأخير: {row['total_late_minutes']}",
                 f"المكافآت: {row['bonuses']:,} د.ع.",
                 f"الاستقطاعات: {row['deductions']:,} د.ع."]
        xlsx_cb = f"act:report_xlsx:perf:{oid}:{uid}"
        rows = [[InlineKeyboardButton("⬇️ تنزيل Excel",
                                      callback_data=xlsx_cb)],
                [InlineKeyboardButton("◀️ التقارير",
                                      callback_data=f"menu:reports_off:{oid}")]]
        context.user_data["perf_range"] = {"start": start, "end": end}
        await _reply(update, "\n".join(lines), InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["act", "report_xlsx"] and len(parts) >= 5:
        kind, oid = parts[2], int(parts[3])
        import tempfile
        from datetime import date, timedelta
        if kind == "daily":
            day = parts[4]
            rep = reports_svc.daily_report(data_dir, actor, office_id=oid,
                                           day=day)
            fname = f"daily_{oid}_{day}.xlsx"
        elif kind == "weekly":
            monday = parts[4]
            rep = reports_svc.weekly_report(data_dir, actor, office_id=oid,
                                            week_start=monday)
            fname = f"weekly_{oid}_{monday}.xlsx"
        else:  # perf
            uid = int(parts[4])
            pr = context.user_data.get("perf_range") or {}
            end = pr.get("end", date.today().isoformat())
            start = pr.get("start",
                           (date.today() - timedelta(days=30)).isoformat())
            rep = reports_svc.rep_performance(data_dir, actor, office_id=oid,
                                              rep_user_id=uid,
                                              start_date=start, end_date=end)
            fname = f"perf_{oid}_{uid}_{start}_{end}.xlsx"
        tmp = Path(tempfile.mkdtemp(prefix="medrep_xlsx_")) / fname
        reports_svc.export_excel(rep, tmp)
        await _reply_document(update, tmp, fname)
        return True

    # ---- products ----
    if data == "menu:products":
        with dbm.identity_session(data_dir) as s:
            offices = s.query(im.Office).order_by(im.Office.id).all()
        await _reply(update, "💊 اختر المكتب:",
                     keyboards.offices_pick("menu:products_off", offices))
        return True

    if parts[:2] == ["menu", "products_off"] and len(parts) == 3:
        oid = int(parts[2])
        prods = products_svc.list_products(data_dir, actor, office_id=oid,
                                           active_only=False)
        rows = []
        for p in prods[:20]:
            mark = "✅" if p["status"] == "active" else "⏸️"
            rows.append([InlineKeyboardButton(
                f"{mark} {p['trade_name']}",
                callback_data=f"menu:product:{oid}:{p['id']}")])
        rows.append([InlineKeyboardButton(
            "➕ منتج جديد", callback_data=f"act:product_add:{oid}")])
        rows.append([InlineKeyboardButton(
            "◀️ المنتجات", callback_data="menu:products")])
        await _reply(update, "💊 *المنتجات*:", InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "product"] and len(parts) == 4:
        oid, pid = int(parts[2]), int(parts[3])
        prods = {p["id"]: p for p in products_svc.list_products(
            data_dir, actor, office_id=oid, active_only=False)}
        p = prods.get(pid)
        if p is None:
            raise DomainError("المنتج غير موجود")
        lines = ["💊 *المنتج*",
                 f"الاسم التجاري: {p['trade_name']}",
                 f"الاسم العلمي: {p['scientific_name'] or '—'}",
                 f"الشركة المصنعة: {p['manufacturer'] or '—'}",
                 f"الحالة: {p['status']}"]
        await _reply(update, "\n".join(lines),
                     keyboards.product_detail_keyboard(
                         oid, pid, p["status"] == "active"))
        return True

    if parts[:2] == ["act", "product_toggle"] and len(parts) == 4:
        oid, pid = int(parts[2]), int(parts[3])
        prods = {p["id"]: p for p in products_svc.list_products(
            data_dir, actor, office_id=oid, active_only=False)}
        p = prods.get(pid)
        if p is None:
            raise DomainError("المنتج غير موجود")
        verb = "تعطيل" if p["status"] == "active" else "تفعيل"
        await _reply(update, f"❓ {verb} المنتج *{p['trade_name']}*؟",
                     keyboards.confirm_keyboard(
                         f"act:product_toggle_yes:{oid}:{pid}",
                         f"menu:product:{oid}:{pid}"))
        return True

    if parts[:2] == ["act", "product_toggle_yes"] and len(parts) == 4:
        oid, pid = int(parts[2]), int(parts[3])
        prods = {p["id"]: p for p in products_svc.list_products(
            data_dir, actor, office_id=oid, active_only=False)}
        p = prods.get(pid)
        if p is None:
            raise DomainError("المنتج غير موجود")
        out = products_svc.set_product_status(
            data_dir, actor, office_id=oid, product_id=pid,
            active=(p["status"] != "active"))
        await _reply(update, f"✅ أصبح المنتج: {out['status']}.",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "product_add"] and len(parts) == 3:
        oid = int(parts[2])
        _set_flow(context, "product_add", "trade_name", {"oid": oid})
        await _reply(update, "💊 أرسل الاسم التجاري للمنتج:",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "product_add_yes"] and len(parts) == 3:
        oid = int(parts[2])
        pending = context.user_data.pop("pending_product", None) or {}
        out = products_svc.create_product(
            data_dir, actor, office_id=oid,
            trade_name=pending.get("trade_name", ""),
            scientific_name=pending.get("scientific_name", ""))
        await _reply(update, f"💊 تم إنشاء المنتج: {out['trade_name']}",
                     keyboards.back_to_main())
        return True

    # ---- surveys ----
    if data == "menu:surveys":
        with dbm.identity_session(data_dir) as s:
            offices = s.query(im.Office).order_by(im.Office.id).all()
        await _reply(update, "📝 اختر المكتب:",
                     keyboards.offices_pick("menu:surveys_off", offices))
        return True

    if parts[:2] == ["menu", "surveys_off"] and len(parts) == 3:
        oid = int(parts[2])
        svs = surveys_svc.list_surveys(data_dir, actor, office_id=oid,
                                       active_only=False)
        rows = []
        for sv in svs[:20]:
            mark = {"draft": "📝", "active": "✅", "closed": "⏹️"}.get(
                sv["status"], "•")
            rows.append([InlineKeyboardButton(
                f"{mark} {sv['title']} ({sv['status']})",
                callback_data=f"menu:survey:{oid}:{sv['survey_id']}")])
        if not rows:
            rows.append([InlineKeyboardButton("لا توجد استبيانات",
                                              callback_data="menu:surveys")])
        rows.append([InlineKeyboardButton("◀️ الاستبيانات",
                                          callback_data="menu:surveys")])
        await _reply(update, "📝 *الاستبيانات*:", InlineKeyboardMarkup(rows))
        return True

    if parts[:2] == ["menu", "survey"] and len(parts) == 4:
        oid, sid = int(parts[2]), int(parts[3])
        res = surveys_svc.survey_results(data_dir, actor, office_id=oid,
                                         survey_id=sid)
        svs = {s["survey_id"]: s for s in surveys_svc.list_surveys(
            data_dir, actor, office_id=oid, active_only=False)}
        status = svs.get(sid, {}).get("status", "")
        lines = [f"📝 *{res['title']}*",
                 f"الحالة: {status or '—'}",
                 f"الأسئلة: {len(res['questions'])}"]
        for q in res["questions"][:10]:
            lines.append(f"• {q['text']} — {q['responses']} إجابات")
        await _reply(update, "\n".join(lines),
                     keyboards.survey_detail_keyboard(oid, sid, status))
        return True

    if parts[:2] == ["act", "survey_activate"] and len(parts) == 4:
        oid, sid = int(parts[2]), int(parts[3])
        await _reply(update, "▶️ تفعيل هذا الاستبيان؟ _سيصبح متاحًا للتعبئة._",
                     keyboards.confirm_keyboard(
                         f"act:survey_activate_yes:{oid}:{sid}",
                         f"menu:survey:{oid}:{sid}"))
        return True

    if parts[:2] == ["act", "survey_activate_yes"] and len(parts) == 4:
        oid, sid = int(parts[2]), int(parts[3])
        out = surveys_svc.set_survey_status(data_dir, actor, office_id=oid,
                                            survey_id=sid, status="active")
        await _reply(update, f"✅ تم تفعيل الاستبيان ({out['status']}).",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["act", "survey_close"] and len(parts) == 4:
        oid, sid = int(parts[2]), int(parts[3])
        await _reply(update, "⏹️ إغلاق هذا الاستبيان؟ _لن تُقبل إجابات جديدة._",
                     keyboards.confirm_keyboard(
                         f"act:survey_close_yes:{oid}:{sid}",
                         f"menu:survey:{oid}:{sid}"))
        return True

    if parts[:2] == ["act", "survey_close_yes"] and len(parts) == 4:
        oid, sid = int(parts[2]), int(parts[3])
        out = surveys_svc.set_survey_status(data_dir, actor, office_id=oid,
                                            survey_id=sid, status="closed")
        await _reply(update, f"⏹️ تم إغلاق الاستبيان ({out['status']}).",
                     keyboards.back_to_main())
        return True

    if parts[:2] == ["menu", "survey_results"] and len(parts) == 4:
        oid, sid = int(parts[2]), int(parts[3])
        res = surveys_svc.survey_results(data_dir, actor, office_id=oid,
                                         survey_id=sid)
        lines = [f"📊 *نتائج: {res['title']}*", ""]
        for q in res["questions"]:
            lines.append(f"❓ {q['text']} ({q['responses']} إجابات)")
            if q["qtype"] == "rating" and q.get("average") is not None:
                lines.append(f"   المتوسط: {q['average']:.1f}")
            for opt, cnt in (q.get("counts") or {}).items():
                lines.append(f"   • {opt}: {cnt}")
        await _reply(update, "\n".join(lines), keyboards.back_to_main())
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
