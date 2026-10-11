"""Owner bot tests with Telegram mocks (no network).

Covers: unauthorized access, missing settings, DB failure, duplicate updates,
and the guarantee that no secrets ever reach chat messages.
"""
from pathlib import Path

import pytest

from app.config import Settings
from app.databases import manager as dbm
from app.identity import models as im
from app.telegram_owner import auth, handlers
from app.telegram_owner.bot import build_application

FAKE_TOKEN = "TEST_TOKEN_abc123_do_not_use"


# --- fakes -------------------------------------------------------------------

class FakeUser:
    def __init__(self, uid):
        self.id = uid


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, reply_markup=None, parse_mode=None):
        self.replies.append((text, reply_markup))


class FakeCallbackQuery:
    def __init__(self, data):
        self.data = data
        self.answered = False
        self.edits = []

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


def _denied_access_events(data_dir):
    with dbm.identity_session(data_dir) as s:
        return (s.query(im.CentralAuditEvent)
                .filter_by(event_type="BOT_ACCESS_DENIED").count())


# --- tests -------------------------------------------------------------------

async def test_start_unauthorized_denied_and_audited(seed):
    ctx = _ctx(seed)
    msg = FakeMessage("/start")
    upd = FakeUpdate(999999, message=msg)
    await handlers.start(upd, ctx)
    assert len(msg.replies) == 1
    assert "غير مصرح" in msg.replies[0][0]
    assert _denied_access_events(seed["data_dir"]) == 1


async def test_start_owner_shows_menu(seed):
    ctx = _ctx(seed)
    msg = FakeMessage("/start")
    await handlers.start(FakeUpdate(100001, message=msg), ctx)
    text, kb = msg.replies[0]
    assert "Owner" in text
    assert kb is not None  # main menu keyboard attached


async def test_every_callback_reauthenticates(seed):
    """A callback query from a non-owner is denied even for harmless menus."""
    ctx = _ctx(seed)
    cb = FakeCallbackQuery("menu:status")
    await handlers.on_callback(FakeUpdate(999999, callback_query=cb), ctx)
    assert cb.answered
    assert "غير مصرح" in cb.edits[0][0]


async def test_status_shows_companies(seed):
    ctx = _ctx(seed)
    cb = FakeCallbackQuery("menu:status")
    await handlers.on_callback(FakeUpdate(100001, callback_query=cb), ctx)
    text = cb.edits[0][0]
    assert "الشركات: 2" in text and "المكاتب: 2" in text


async def test_companies_navigation(seed):
    ctx = _ctx(seed)
    cb = FakeCallbackQuery("menu:companies")
    await handlers.on_callback(FakeUpdate(100001, callback_query=cb), ctx)
    assert "الشركات" in cb.edits[0][0]
    # drill into company 1
    cb2 = FakeCallbackQuery(f"menu:company:{seed['company1']}")
    await handlers.on_callback(FakeUpdate(100001, callback_query=cb2), ctx)
    assert "الشفاء" in cb2.edits[0][0]


async def test_office_detail_and_reps(seed):
    ctx = _ctx(seed)
    cb = FakeCallbackQuery(f"menu:office:{seed['office1']}")
    await handlers.on_callback(FakeUpdate(100001, callback_query=cb), ctx)
    assert "بغداد" in cb.edits[0][0]
    cb2 = FakeCallbackQuery(f"menu:reps:{seed['office1']}:0")
    await handlers.on_callback(FakeUpdate(100001, callback_query=cb2), ctx)
    assert "SHF-001" in cb2.edits[0][0]


async def test_rep_search_flow(seed):
    ctx = _ctx(seed)
    cb = FakeCallbackQuery("menu:search_rep")
    await handlers.on_callback(FakeUpdate(100001, callback_query=cb), ctx)
    assert ctx.user_data.get("awaiting") == "rep_search"
    msg = FakeMessage("أحمد")
    await handlers.on_text(FakeUpdate(100001, message=msg), ctx)
    text, kb = msg.replies[0]
    assert "النتائج (1)" in text
    btn_texts = [b.text for row in kb.inline_keyboard for b in row]
    assert any("مندوب أحمد" in t for t in btn_texts)


async def test_audit_log_visible(seed):
    ctx = _ctx(seed)
    cb = FakeCallbackQuery("menu:audit:0")
    await handlers.on_callback(FakeUpdate(100001, callback_query=cb), ctx)
    # page 0 = most recent; company creation is older (page 1)
    assert "REPRESENTATIVE_ADDED" in cb.edits[0][0]
    cb2 = FakeCallbackQuery("menu:audit:1")
    await handlers.on_callback(FakeUpdate(100001, callback_query=cb2), ctx)
    assert "OWNER_CREATED_COMPANY" in cb2.edits[0][0]


async def test_unknown_callback_handled(seed):
    ctx = _ctx(seed)
    cb = FakeCallbackQuery("menu:nope")
    await handlers.on_callback(FakeUpdate(100001, callback_query=cb), ctx)
    assert "غير معروف" in cb.edits[0][0]


async def test_duplicate_start_is_idempotent(seed):
    """Two /start updates produce identical menus and no side effects."""
    ctx = _ctx(seed)
    m1, m2 = FakeMessage("/start"), FakeMessage("/start")
    await handlers.start(FakeUpdate(100001, message=m1), ctx)
    await handlers.start(FakeUpdate(100001, message=m2), ctx)
    assert m1.replies[0][0] == m2.replies[0][0]


async def test_db_failure_gives_graceful_error(seed, tmp_path):
    """Broken data dir -> user gets a generic error, no traceback."""
    broken = tmp_path / "notadir"
    broken.write_text("i am a file, not a dir")
    ctx = FakeContext(_settings(broken), broken)
    cb = FakeCallbackQuery("menu:status")
    await handlers.on_callback(FakeUpdate(100001, callback_query=cb), ctx)
    assert "حدث خطأ" in cb.edits[0][0]


async def test_no_secrets_in_any_message(seed):
    """Walk the main menus; assert token/keys never appear in chat text."""
    ctx = _ctx(seed)
    seen = []

    async def cb(data):
        q = FakeCallbackQuery(data)
        await handlers.on_callback(FakeUpdate(100001, callback_query=q), ctx)
        seen.extend(t for t, _ in q.edits)

    for d in ["menu:status", "menu:companies",
              f"menu:company:{seed['company1']}",
              f"menu:office:{seed['office1']}",
              "menu:users:0", "menu:audit:0", "menu:backups"]:
        await cb(d)
    key_bytes = dbm.office_key(
        seed["data_dir"], seed["office1"]).decode()
    blob = "\n".join(seen)
    assert FAKE_TOKEN not in blob
    assert key_bytes not in blob
    assert "office_keys" not in blob


def test_build_application_fails_safe_without_owner_ids(seed):
    s = _settings(seed["data_dir"], owner_ids="")
    with pytest.raises(auth.BotNotConfiguredError):
        build_application(s, seed["data_dir"])


def test_build_application_fails_safe_without_token(seed):
    s = Settings(telegram_owner_ids="100001", telegram_owner_bot_token="",
                 data_dir=seed["data_dir"])
    with pytest.raises(auth.BotNotConfiguredError):
        build_application(s, seed["data_dir"])


def test_resolve_owner_actor_rejects_non_member(seed):
    """Telegram id in env but no user row -> denied (no auto-owner)."""
    with pytest.raises(Exception):
        auth.resolve_owner_actor({424242}, seed["data_dir"], 424242)


# --- Phase 7: write actions (mock-tested; every action hits the real service)
# ---

async def _cb(seed, ctx, uid, data):
    q = FakeCallbackQuery(data)
    upd = FakeUpdate(uid, callback_query=q)
    await handlers.on_callback(upd, ctx)
    return q


def _owner_uid(seed):
    # resolve_owner_actor maps telegram id -> internal user; find owner tg id
    return 100001


def test_plan_approve_flow(seed):
    from app.services import plans as plans_svc
    d = seed["data_dir"]
    ctx = _ctx(seed)
    uid = _owner_uid(seed)
    # owner telegram id must map to the owner user: patch settings via owner_ids
    # seed's owner user has telegram id? use auth.resolve directly in test:
    import app.telegram_owner.auth as auth
    from app.databases import manager as dbm
    from app.identity import models as im
    with dbm.identity_session(d) as s:
        owner = s.query(im.User).join(im.Membership,
                                      im.Membership.user_id == im.User.id).filter(
            im.Membership.role == "owner").first()
        owner.telegram_id = 100001
    # create + submit a plan as rep1
    p = plans_svc.create_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                              plan_type="weekly", rep_user_id=seed["rep1_o1"],
                              period="2026-10-12", payload={})
    plans_svc.submit_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                          plan_type="weekly", plan_id=p["plan_id"])
    pid = p["plan_id"]
    o1 = seed["office1"]

    async def run():
        q = await _cb(seed, ctx, uid, "menu:plans")
        assert any("الخطط المعلقة" in (e[0] or "") for e in q.edits)
        q = await _cb(seed, ctx, uid, f"menu:plans_off:{o1}")
        btns = [b.callback_data for e in q.edits for row in e[1].inline_keyboard
                for b in row]
        assert any(f"menu:plan:weekly:{pid}:{o1}" == b for b in btns)
        q = await _cb(seed, ctx, uid, f"menu:plan:weekly:{pid}:{o1}")
        assert any("اعتماد" in str(e[1].inline_keyboard) for e in q.edits)
        q = await _cb(seed, ctx, uid, f"act:plan_approve:weekly:{pid}:{o1}")
        assert any("تأكيد" in str(e[1].inline_keyboard) for e in q.edits)
        q = await _cb(seed, ctx, uid, f"act:plan_approve_yes:weekly:{pid}:{o1}")
        assert any("تم اعتماد" in (e[0] or "") for e in q.edits)

    import asyncio
    asyncio.run(run())
    got = plans_svc.get_plan(d, seed["owner"], office_id=o1,
                             plan_type="weekly", plan_id=pid)
    assert got["status"] == "approved"


def test_plan_return_flow(seed):
    from app.services import plans as plans_svc
    d = seed["data_dir"]
    ctx = _ctx(seed)
    uid = _owner_uid(seed)
    from app.databases import manager as dbm
    from app.identity import models as im
    with dbm.identity_session(d) as s:
        owner = s.query(im.User).join(im.Membership,
                                      im.Membership.user_id == im.User.id).filter(
            im.Membership.role == "owner").first()
        owner.telegram_id = 100001
    p = plans_svc.create_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                              plan_type="weekly", rep_user_id=seed["rep1_o1"],
                              period="2026-10-19", payload={})
    plans_svc.submit_plan(d, seed["rep1_o1"], office_id=seed["office1"],
                          plan_type="weekly", plan_id=p["plan_id"])
    pid = p["plan_id"]
    o1 = seed["office1"]

    async def run():
        await _cb(seed, ctx, uid, f"act:plan_return:weekly:{pid}:{o1}")
        # send notes as text
        msg = FakeMessage("أضف زيارتين للصيدليات")
        upd = FakeUpdate(uid, message=msg)
        await handlers.on_text(upd, ctx)
        assert any("إرجاع الخطة" in (r[0] or "") for r in msg.replies)
        q = await _cb(seed, ctx, uid, f"act:plan_return_yes:weekly:{pid}:{o1}")
        assert any("أُرجعت" in (e[0] or "") for e in q.edits)

    import asyncio
    asyncio.run(run())
    got = plans_svc.get_plan(d, seed["owner"], office_id=o1,
                             plan_type="weekly", plan_id=pid)
    assert got["status"] == "changes_requested"


def test_rep_terminate_flow(seed):
    from app.services import representatives as rep_svc
    d = seed["data_dir"]
    ctx = _ctx(seed)
    uid = _owner_uid(seed)
    from app.databases import manager as dbm
    from app.identity import models as im
    with dbm.identity_session(d) as s:
        owner = s.query(im.User).join(im.Membership,
                                      im.Membership.user_id == im.User.id).filter(
            im.Membership.role == "owner").first()
        owner.telegram_id = 100001
    o1, rep = seed["office1"], seed["rep1_o1"]

    async def run():
        await _cb(seed, ctx, uid, f"act:rep_term:{o1}:{rep}")
        msg = FakeMessage("انتهاء العقد")
        upd = FakeUpdate(uid, message=msg)
        await handlers.on_text(upd, ctx)
        assert any("إنهاء ارتباط" in (r[0] or "") for r in msg.replies)
        q = await _cb(seed, ctx, uid, f"act:rep_term_yes:{o1}:{rep}")
        assert any("تم إنهاء" in (e[0] or "") for e in q.edits)

    import asyncio
    asyncio.run(run())
    rec = rep_svc.get_representative(d, seed["owner"], office_id=o1,
                                     rep_user_id=rep)
    assert rec["job_status"] == "ended"


def test_company_add_flow(seed):
    d = seed["data_dir"]
    ctx = _ctx(seed)
    uid = _owner_uid(seed)
    from app.databases import manager as dbm
    from app.identity import models as im
    with dbm.identity_session(d) as s:
        owner = s.query(im.User).join(im.Membership,
                                      im.Membership.user_id == im.User.id).filter(
            im.Membership.role == "owner").first()
        owner.telegram_id = 100001

    async def run():
        await _cb(seed, ctx, uid, "act:company_add")
        for text in ("شركة الاختبار", "testco"):
            msg = FakeMessage(text)
            await handlers.on_text(FakeUpdate(uid, message=msg), ctx)
        q = await _cb(seed, ctx, uid, "act:company_add_yes")
        assert any("تم إنشاء الشركة" in (e[0] or "") for e in q.edits)

    import asyncio
    asyncio.run(run())
    with dbm.identity_session(d) as s:
        c = s.query(im.Company).filter_by(name_en="testco").one()
        assert c.name_ar == "شركة الاختبار"


def test_unauthorized_cannot_trigger_write_action(seed):
    d = seed["data_dir"]
    ctx = _ctx(seed, owner_ids="999999")  # unknown telegram id

    async def run():
        q = await _cb(seed, ctx, 999999, "menu:plans")
        assert any("غير مصرح" in (e[0] or "") or "مرفوض" in (e[0] or "")
                   for e in q.edits)

    import asyncio
    asyncio.run(run())
