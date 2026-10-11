"""Owner bot extended actions (Phase B): leaves, finance, reports,
products, surveys. All Telegram interactions are mocked (no network);
every write action hits the real service and is verified in the DB.

Key discipline: tests press the callback_data produced by the keyboard
buttons themselves, so dead callbacks are caught.
"""
import uuid
from pathlib import Path

import pytest

from app.config import Settings
from app.databases import manager as dbm
from app.databases import office_models as om
from app.identity import models as im
from app.telegram_owner import handlers, keyboards

FAKE_TOKEN = "TEST_TOKEN_abc123_do_not_use"


# --- fakes -------------------------------------------------------------------

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []
        self.documents = []

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        self.replies.append((text, reply_markup))

    async def reply_document(self, document, filename=None, **kwargs):
        data = document.read() if hasattr(document, "read") else document
        self.documents.append((filename, data))


class FakeCallbackQuery:
    def __init__(self, data, message=None):
        self.data = data
        self.answered = False
        self.edits = []
        self.message = message if message is not None else FakeMessage()

    async def answer(self):
        self.answered = True

    async def edit_message_text(self, text, reply_markup=None, parse_mode=None):
        self.edits.append((text, reply_markup))


class FakeUpdate:
    def __init__(self, uid, message=None, callback_query=None):
        self.effective_user = FakeUser(uid)
        self.message = message
        self.callback_query = callback_query


class FakeContext:
    def __init__(self, settings, data_dir):
        self.bot_data = {"settings": settings, "data_dir": data_dir}
        self.user_data = {}


def _settings(data_dir: Path, owner_ids="100001"):
    return Settings(telegram_owner_ids=owner_ids,
                    telegram_owner_bot_token=FAKE_TOKEN,
                    data_dir=data_dir)


def _ctx(seed, owner_ids="100001"):
    return FakeContext(_settings(seed["data_dir"], owner_ids), seed["data_dir"])


def _make_owner(seed, tg_id=100001):
    d = seed["data_dir"]
    with dbm.identity_session(d) as s:
        owner = (s.query(im.User).join(im.Membership,
                                       im.Membership.user_id == im.User.id)
                 .filter(im.Membership.role == "owner").first())
        owner.telegram_id = tg_id


async def _cb(ctx, uid, data, message=None):
    q = FakeCallbackQuery(data, message=message)
    await handlers.on_callback(FakeUpdate(uid, callback_query=q), ctx)
    return q


async def _text(ctx, uid, text):
    msg = FakeMessage(text)
    await handlers.on_text(FakeUpdate(uid, message=msg), ctx)
    return msg


def _buttons(kb):
    return [(b.text, b.callback_data)
            for row in kb.inline_keyboard for b in row]


def _find_cb(kb, text_part):
    for text, cb in _buttons(kb):
        if text_part in text:
            return cb
    raise AssertionError(f"button {text_part!r} not found in "
                         f"{[t for t, _ in _buttons(kb)]}")


def _last_kb(q):
    assert q.edits, "no message edits produced"
    return q.edits[-1][1]


# --- leaves ------------------------------------------------------------------

def _make_leave_request(seed):
    from app.services import leave as leave_svc
    d, o1 = seed["data_dir"], seed["office1"]
    lt = leave_svc.create_leave_type(
        d, seed["manager1"], office_id=o1, name="سنوية",
        default_days_per_year=21)
    return leave_svc.request_leave(
        d, seed["rep1_o1"], office_id=o1, rep_user_id=seed["rep1_o1"],
        leave_type_id=lt["id"],
        start_date="2026-11-01", end_date="2026-11-03", reason="راحة")


async def test_leave_approve_flow(seed):
    d, o1, uid = seed["data_dir"], seed["office1"], 100001
    _make_owner(seed)
    req = _make_leave_request(seed)
    rid = req["request_id"]
    ctx = _ctx(seed)

    q = await _cb(ctx, uid, "menu:leaves")
    assert "طلبات الإجازة" in q.edits[0][0]
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "بغداد"))
    assert f"menu:leaves_off:{o1}" in q.data
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "2026-11-01"))
    assert q.data == f"menu:leave:{o1}:{rid}"
    assert "طلب إجازة" in q.edits[0][0]
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "اعتماد الإجازة"))
    assert q.data == f"act:leave_approve:{o1}:{rid}"
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "تأكيد"))
    assert q.data == f"act:leave_approve_yes:{o1}:{rid}"
    assert any("تم اعتماد الإجازة" in (e[0] or "") for e in q.edits)

    with dbm.office_session(d, o1) as osess:
        assert osess.get(om.LeaveRequest, rid).status == "approved"


async def test_leave_reject_flow_with_notes(seed):
    d, o1, uid = seed["data_dir"], seed["office1"], 100001
    _make_owner(seed)
    rid = _make_leave_request(seed)["request_id"]
    ctx = _ctx(seed)

    q = await _cb(ctx, uid, f"menu:leave:{o1}:{rid}")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "رفض الإجازة"))
    msg = await _text(ctx, uid, "لا يوجد بديل")
    assert "رفض طلب الإجازة" in msg.replies[0][0]
    q = await _cb(ctx, uid, _find_cb(msg.replies[0][1], "تأكيد"))
    assert any("تم رفض طلب الإجازة" in (e[0] or "") for e in q.edits)

    with dbm.office_session(d, o1) as osess:
        r = osess.get(om.LeaveRequest, rid)
        assert r.status == "rejected"
        assert r.review_notes == "لا يوجد بديل"


async def test_leave_unauthorized_denied(seed):
    _make_owner(seed)
    rid = _make_leave_request(seed)["request_id"]
    ctx = _ctx(seed, owner_ids="999999")
    q = await _cb(ctx, 999999, f"act:leave_approve_yes:{seed['office1']}:{rid}")
    assert any("غير مصرح" in (e[0] or "") for e in q.edits)


# --- finance -----------------------------------------------------------------

def _make_proposal(seed):
    from app.services import payroll as payroll_svc
    d, o1 = seed["data_dir"], seed["office1"]
    p = payroll_svc.open_payroll_period(d, seed["manager1"], office_id=o1,
                                        year=2026, month=11)
    return payroll_svc.propose_deduction(
        d, seed["manager1"], office_id=o1, rep_user_id=seed["rep1_o1"],
        period_id=p["period_id"], amount=50000, reason="تأخير متكرر",
        idempotency_key=f"bot-test-{uuid.uuid4()}")


async def test_deduct_approve_flow(seed):
    d, o1, uid = seed["data_dir"], seed["office1"], 100001
    _make_owner(seed)
    txn = _make_proposal(seed)
    tx_uuid = txn["uuid"]
    ctx = _ctx(seed)

    q = await _cb(ctx, uid, "menu:finance")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "الخصومات المقترحة"))
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "بغداد"))
    assert q.data == f"menu:deducts_off:{o1}"
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "50,000"))
    assert q.data == f"menu:deduct:{o1}:{tx_uuid}"
    assert "خصم مقترح" in q.edits[0][0]
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "اعتماد الخصم"))
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "تأكيد"))
    assert any("تم اعتماد الخصم" in (e[0] or "") for e in q.edits)

    with dbm.office_session(d, o1) as osess:
        t = osess.query(om.FinancialTransaction).filter_by(uuid=tx_uuid).one()
        assert t.status == "applied"


async def test_deduct_reject_flow(seed):
    d, o1, uid = seed["data_dir"], seed["office1"], 100001
    _make_owner(seed)
    tx_uuid = _make_proposal(seed)["uuid"]
    ctx = _ctx(seed)

    q = await _cb(ctx, uid, f"menu:deduct:{o1}:{tx_uuid}")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "رفض الخصم"))
    msg = await _text(ctx, uid, "غير مبرر")
    q = await _cb(ctx, uid, _find_cb(msg.replies[0][1], "تأكيد"))
    assert any("تم رفض الخصم" in (e[0] or "") for e in q.edits)

    with dbm.office_session(d, o1) as osess:
        t = osess.query(om.FinancialTransaction).filter_by(uuid=tx_uuid).one()
        assert t.status == "rejected"


async def test_salary_summary_view(seed):
    from app.services import payroll as payroll_svc
    d, o1, uid = seed["data_dir"], seed["office1"], 100001
    _make_owner(seed)
    rep = seed["rep1_o1"]
    p = payroll_svc.open_payroll_period(d, seed["manager1"], office_id=o1,
                                        year=2026, month=10)
    payroll_svc.set_base_salary(d, seed["manager1"], office_id=o1,
                                period_id=p["period_id"], rep_user_id=rep,
                                amount=1000000)
    ctx = _ctx(seed)

    q = await _cb(ctx, uid, "menu:finance")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "ملخص راتب"))
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "بغداد"))
    assert q.data == f"menu:salary_off:{o1}"
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "مندوب أحمد"))
    assert q.data == f"menu:salary_rep:{o1}:{rep}"
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "2026-10"))
    assert q.data == f"menu:salary_view:{o1}:{rep}:{p['period_id']}"
    text = q.edits[0][0]
    assert "ملخص الراتب" in text and "1,000,000" in text and "الصافي" in text


# --- reports -----------------------------------------------------------------

async def test_daily_report_and_xlsx_download(seed):
    d, o1, uid = seed["data_dir"], seed["office1"], 100001
    _make_owner(seed)
    ctx = _ctx(seed)

    q = await _cb(ctx, uid, "menu:reports")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "بغداد"))
    assert q.data == f"menu:reports_off:{o1}"
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "اليومي"))
    assert "التقرير اليومي" in q.edits[0][0]
    assert "الزيارات" in q.edits[0][0]
    xlsx_cb = _find_cb(_last_kb(q), "Excel")
    assert xlsx_cb.startswith("act:report_xlsx:daily:")
    msg = FakeMessage()
    q = await _cb(ctx, uid, xlsx_cb, message=msg)
    assert len(msg.documents) == 1
    fname, blob = msg.documents[0]
    assert fname.endswith(".xlsx") and blob[:2] == b"PK"  # zip = xlsx


async def test_weekly_report_text(seed):
    o1, uid = seed["office1"], 100001
    _make_owner(seed)
    ctx = _ctx(seed)
    q = await _cb(ctx, uid, f"menu:reports_off:{o1}")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "الأسبوعي"))
    assert "التقرير الأسبوعي" in q.edits[0][0]


async def test_rep_performance_report(seed):
    o1, uid = seed["office1"], 100001
    _make_owner(seed)
    ctx = _ctx(seed)
    q = await _cb(ctx, uid, f"menu:reports_off:{o1}")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "أداء مندوب"))
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "مندوب أحمد"))
    text = q.edits[0][0]
    assert "أداء" in text and "الزيارات" in text
    xlsx_cb = _find_cb(_last_kb(q), "Excel")
    msg = FakeMessage()
    q = await _cb(ctx, uid, xlsx_cb, message=msg)
    assert len(msg.documents) == 1 and msg.documents[0][0].endswith(".xlsx")


# --- products ----------------------------------------------------------------

async def test_product_add_and_toggle_flow(seed):
    from app.services import products as products_svc
    d, o1, uid = seed["data_dir"], seed["office1"], 100001
    _make_owner(seed)
    ctx = _ctx(seed)

    q = await _cb(ctx, uid, "menu:products")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "بغداد"))
    assert q.data == f"menu:products_off:{o1}"
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "منتج جديد"))
    assert q.data == f"act:product_add:{o1}"
    msg = await _text(ctx, uid, "بانادول")
    assert "الاسم العلمي" in msg.replies[0][0]
    msg = await _text(ctx, uid, "باراسيتامول")
    assert "إنشاء منتج" in msg.replies[0][0]
    q = await _cb(ctx, uid, _find_cb(msg.replies[0][1], "تأكيد"))
    assert any("تم إنشاء المنتج" in (e[0] or "") for e in q.edits)

    prods = products_svc.list_products(d, seed["owner"], office_id=o1,
                                       active_only=False)
    p = next(x for x in prods if x["trade_name"] == "بانادول")
    assert p["scientific_name"] == "باراسيتامول"

    # toggle off via the product detail buttons
    q = await _cb(ctx, uid, f"menu:product:{o1}:{p['id']}")
    assert "بانادول" in q.edits[0][0]
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "تعطيل"))
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "تأكيد"))
    assert any("أصبح المنتج" in (e[0] or "") for e in q.edits)
    prods = products_svc.list_products(d, seed["owner"], office_id=o1,
                                       active_only=False)
    assert next(x for x in prods if x["id"] == p["id"])["status"] == "inactive"


# --- surveys -----------------------------------------------------------------

def _make_survey(seed):
    from app.services import surveys as surveys_svc
    d, o1 = seed["data_dir"], seed["office1"]
    sv = surveys_svc.create_survey(d, seed["manager1"], office_id=o1,
                                   title="رضا الأطباء")
    surveys_svc.add_question(d, seed["manager1"], office_id=o1,
                             survey_id=sv["survey_id"],
                             text="قيّم الخدمة", qtype="rating")
    return sv["survey_id"]


async def test_survey_activate_and_results(seed):
    from app.services import surveys as surveys_svc
    d, o1, uid = seed["data_dir"], seed["office1"], 100001
    _make_owner(seed)
    sid = _make_survey(seed)
    ctx = _ctx(seed)

    q = await _cb(ctx, uid, "menu:surveys")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "بغداد"))
    assert q.data == f"menu:surveys_off:{o1}"
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "رضا الأطباء"))
    assert q.data == f"menu:survey:{o1}:{sid}"
    assert "رضا الأطباء" in q.edits[0][0]
    # detail keyboard built from real buttons: activate (draft status)
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "تفعيل"))
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "تأكيد"))
    assert any("تم تفعيل الاستبيان" in (e[0] or "") for e in q.edits)
    svs = {s["survey_id"]: s for s in surveys_svc.list_surveys(
        d, seed["owner"], office_id=o1, active_only=False)}
    assert svs[sid]["status"] == "active"

    # results view via the results button
    q = await _cb(ctx, uid, f"menu:survey:{o1}:{sid}")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "عرض النتائج"))
    assert "نتائج" in q.edits[0][0] and "قيّم الخدمة" in q.edits[0][0]


async def test_survey_close_flow(seed):
    from app.services import surveys as surveys_svc
    d, o1, uid = seed["data_dir"], seed["office1"], 100001
    _make_owner(seed)
    sid = _make_survey(seed)
    surveys_svc.set_survey_status(d, seed["manager1"], office_id=o1,
                                  survey_id=sid, status="active")
    ctx = _ctx(seed)

    q = await _cb(ctx, uid, f"menu:survey:{o1}:{sid}")
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "إغلاق"))
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "تأكيد"))
    assert any("تم إغلاق الاستبيان" in (e[0] or "") for e in q.edits)
    svs = {s["survey_id"]: s for s in surveys_svc.list_surveys(
        d, seed["owner"], office_id=o1, active_only=False)}
    assert svs[sid]["status"] == "closed"


# --- wiring integrity --------------------------------------------------------

async def test_all_new_main_menu_buttons_are_routed(seed):
    """Every new main-menu button must be handled (no 'unknown command')."""
    _make_owner(seed)
    ctx, uid = _ctx(seed), 100001
    for data in ["menu:leaves", "menu:finance", "menu:reports",
                 "menu:products", "menu:surveys"]:
        q = await _cb(ctx, uid, data)
        assert not any("غير معروف" in (e[0] or "") for e in q.edits), data


async def test_plan_review_keyboard_includes_office(seed):
    """Regression: the plan detail buttons must carry office_id so the
    handler accepts them (previously a dead callback)."""
    from app.services import plans as plans_svc
    d, o1, uid = seed["data_dir"], seed["office1"], 100001
    _make_owner(seed)
    p = plans_svc.create_plan(d, seed["rep1_o1"], office_id=o1,
                              plan_type="weekly", rep_user_id=seed["rep1_o1"],
                              period="2026-12-07", payload={})
    plans_svc.submit_plan(d, seed["rep1_o1"], office_id=o1,
                          plan_type="weekly", plan_id=p["plan_id"])
    pid = p["plan_id"]
    ctx = _ctx(seed)

    q = await _cb(ctx, uid, f"menu:plan:weekly:{pid}:{o1}")
    approve_cb = _find_cb(_last_kb(q), "اعتماد")
    assert approve_cb == f"act:plan_approve:weekly:{pid}:{o1}"
    q = await _cb(ctx, uid, approve_cb)
    assert not any("غير معروف" in (e[0] or "") for e in q.edits)
    q = await _cb(ctx, uid, _find_cb(_last_kb(q), "تأكيد"))
    assert any("تم اعتماد" in (e[0] or "") for e in q.edits)
    got = plans_svc.get_plan(d, seed["owner"], office_id=o1,
                             plan_type="weekly", plan_id=pid)
    assert got["status"] == "approved"


async def test_no_secrets_in_new_messages(seed):
    """Extended menus must never leak token/keys/paths."""
    _make_owner(seed)
    ctx, uid = _ctx(seed), 100001
    o1 = seed["office1"]
    _make_leave_request(seed)
    seen = []

    async def cb(data, message=None):
        q = await _cb(ctx, uid, data, message=message)
        seen.extend(t for t, _ in q.edits)
        return q

    q = await cb("menu:leaves")
    await cb(_find_cb(_last_kb(q), "بغداد"))
    await cb("menu:finance")
    q = await cb("menu:reports")
    await cb(_find_cb(_last_kb(q), "بغداد"))
    await cb("menu:products")
    await cb("menu:surveys")
    q = await cb(f"menu:reports_off:{o1}")
    q = await cb(_find_cb(_last_kb(q), "اليومي"))
    await cb(_find_cb(_last_kb(q), "Excel"), message=FakeMessage())

    key_bytes = dbm.office_key(seed["data_dir"], o1).decode()
    blob = "\n".join(seen)
    assert FAKE_TOKEN not in blob
    assert key_bytes not in blob
    assert "office_keys" not in blob
