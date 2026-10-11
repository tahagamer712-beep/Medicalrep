"""WhatsApp Cloud API integration — all mocks, no network, no real account.

Covers: webhook verification handshake, signature enforcement, dedupe,
24h window/template policy, phone->user resolution, role gate, and the
three flows (daily plan, check-in/out, leave). CloudApiWhatsAppSender is
never invoked here.
"""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.whatsapp import flows, webhook
from app.whatsapp.config import WhatsAppSettings
from app.whatsapp.sender import CloudApiWhatsAppSender, MockWhatsAppSender
from app.whatsapp.templates import (TEMPLATES, send_smart, within_window,
                                    WINDOW)

VERIFY = "test-verify-token"
SECRET = "test-app-secret"
REP_WA = "9647000000011"  # international form of seed rep1_o1 (07000000011)


@pytest.fixture()
def wacfg(monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", VERIFY)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", SECRET)
    cfg = WhatsAppSettings()
    monkeypatch.setattr(webhook, "whatsapp_settings", cfg)
    return cfg


@pytest.fixture()
def sender():
    s = MockWhatsAppSender()
    webhook.set_sender(s)
    yield s
    webhook.set_sender(MockWhatsAppSender())


@pytest.fixture()
def client():
    return TestClient(app)


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body,
                                hashlib.sha256).hexdigest()


def _payload(mid: str, wa_from: str, text: str, msg_type: str = "text") -> dict:
    return {"entry": [{"changes": [{"value": {"messages": [{
        "id": mid, "from": wa_from, "type": msg_type,
        "text": {"body": text}}]}}]}]}


# --- verification handshake ------------------------------------------------

def test_verify_ok(client, wacfg):
    r = client.get("/whatsapp/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": VERIFY,
        "hub.challenge": "CHALLENGE123"})
    assert r.status_code == 200
    assert r.text == "CHALLENGE123"


def test_verify_wrong_token(client, wacfg):
    r = client.get("/whatsapp/webhook", params={
        "hub.mode": "subscribe", "hub.verify_token": "nope",
        "hub.challenge": "CHALLENGE123"})
    assert r.status_code == 403


def test_verify_wrong_mode(client, wacfg):
    r = client.get("/whatsapp/webhook", params={
        "hub.mode": "unsubscribe", "hub.verify_token": VERIFY,
        "hub.challenge": "CHALLENGE123"})
    assert r.status_code == 403


def test_health_still_ok(client):
    assert client.get("/health").status_code == 200


# --- signature enforcement --------------------------------------------------

def test_post_missing_signature_rejected(client, wacfg, seed):
    r = client.post("/whatsapp/webhook", json=_payload("m1", REP_WA, "hi"))
    assert r.status_code == 403


def test_post_bad_signature_rejected(client, wacfg, seed):
    r = client.post("/whatsapp/webhook", json=_payload("m1", REP_WA, "hi"),
                    headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 403


def test_post_invalid_json_rejected(client, wacfg, seed):
    body = b"not json{"
    r = client.post("/whatsapp/webhook", content=body,
                    headers={"X-Hub-Signature-256": _sign(body),
                             "Content-Type": "application/json"})
    assert r.status_code in (400, 422)


def test_post_ok_and_unknown_user_replied(client, wacfg, seed, sender):
    import json as _json
    body = _json.dumps(_payload("m2", "9647900000000", "hi")).encode()
    r = client.post("/whatsapp/webhook", content=body,
                    headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert r.json()["processed"] == 1
    assert sender.texts_to("07900000000") == ["هذا الرقم غير مسجّل في النظام."]


def test_non_text_messages_skipped(client, wacfg, seed, sender):
    import json as _json
    body = _json.dumps(_payload("m3", REP_WA, "", msg_type="image")).encode()
    r = client.post("/whatsapp/webhook", content=body,
                    headers={"X-Hub-Signature-256": _sign(body)})
    assert r.status_code == 200
    assert r.json()["processed"] == 0
    assert sender.sent == []


# --- dedupe ------------------------------------------------------------------

def test_duplicate_message_processed_once(client, wacfg, seed, sender):
    import json as _json
    p = _payload("dup-1", REP_WA, "الخطة")
    body = _json.dumps(p).encode()
    headers = {"X-Hub-Signature-256": _sign(body)}
    r1 = client.post("/whatsapp/webhook", content=body, headers=headers)
    r2 = client.post("/whatsapp/webhook", content=body, headers=headers)
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["processed"] == 1
    assert r2.json()["processed"] == 1  # claimed already; skipped inside
    # exactly one reply for the one real processing
    assert len(sender.sent) == 1


def test_claim_message_atomic(seed):
    from app.whatsapp import dedupe
    d = seed["data_dir"]
    assert dedupe.claim_message(d, "waba-x", seed["rep1_o1"], "07000000011")
    assert not dedupe.claim_message(d, "waba-x", seed["rep1_o1"], "07000000011")
    assert dedupe.was_processed(d, "waba-x")


# --- 24h window / templates ---------------------------------------------------

def test_within_window():
    now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    assert within_window(now - timedelta(hours=23), now)
    assert not within_window(now - timedelta(hours=25), now)
    assert not within_window(None, now)


def test_send_smart_uses_text_inside_window():
    s = MockWhatsAppSender()
    now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    kind, _ = send_smart(s, "9647000000011", "hello", template="daily_plan",
                         template_params=["a", "b"],
                         last_inbound_at=now - timedelta(hours=1), now=now)
    assert kind == "text"
    assert s.texts_to("9647000000011") == ["hello"]


def test_send_smart_uses_template_outside_window():
    s = MockWhatsAppSender()
    now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    kind, _ = send_smart(s, "9647000000011", "hello", template="daily_plan",
                         template_params=["2026-10-11", "x"],
                         last_inbound_at=now - timedelta(days=2), now=now)
    assert kind == "template"
    assert s.templates_to("9647000000011") == ["daily_plan"]


def test_send_smart_unknown_template_rejected():
    s = MockWhatsAppSender()
    now = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)
    with pytest.raises(KeyError):
        send_smart(s, "9647000000011", "hello", template="nope",
                   template_params=[], last_inbound_at=None, now=now)
    assert s.sent == []


def test_template_registry_sane():
    assert set(TEMPLATES) == {"daily_plan", "attendance_confirm",
                              "leave_received"}
    for name, t in TEMPLATES.items():
        assert t["language"] == "ar" and isinstance(t["params"], list)


# --- phone resolution + role gate ---------------------------------------------

def test_normalize_phone():
    assert flows.normalize_phone("+964 700 000 0011") == "07000000011"
    assert flows.normalize_phone("07000000011") == "07000000011"


def test_resolve_rep_context(seed):
    ctx = flows.resolve_context(seed["data_dir"], REP_WA)
    assert ctx is not None
    assert ctx.user_id == seed["rep1_o1"]
    assert ctx.role == "rep"
    assert ctx.office_id == seed["office1"]


def test_resolve_unknown_returns_none(seed):
    assert flows.resolve_context(seed["data_dir"], "9647900000000") is None


def test_owner_role_rejected(seed):
    from app.databases import manager as dbm
    from app.identity import models as im
    d = seed["data_dir"]
    with dbm.identity_session(d) as s:
        u = im.User(full_name="مالك مكتب", phone="07900000001")
        s.add(u)
        s.flush()
        s.add(im.Membership(user_id=u.id, company_id=seed["company1"],
                            office_id=seed["office1"], role="owner"))
    s2 = MockWhatsAppSender()
    res = flows.handle_text(d, s2, "07900000001", "الخطة", "own-1")
    assert res["status"] == "role_rejected"
    assert s2.texts_to("07900000001") == ["هذه القناة للمندوبين والمشرفين والمدراء."]


# --- flows --------------------------------------------------------------------

def _today_baghdad() -> str:
    return datetime.now(ZoneInfo("Asia/Baghdad")).strftime("%Y-%m-%d")


def test_flow_daily_plan(seed):
    from app.services import plans
    d, s = seed["data_dir"], MockWhatsAppSender()
    rep, o1 = seed["rep1_o1"], seed["office1"]
    plans.create_plan(d, rep, office_id=o1, plan_type="daily",
                      rep_user_id=rep, period=_today_baghdad(), payload={})
    res = flows.handle_text(d, s, REP_WA, "الخطة", "plan-1")
    assert res["status"] == "ok" and res["kind"] == "text"
    assert "خطتك اليومية" in s.texts_to("07000000011")[0]


def test_flow_daily_plan_missing(seed):
    d, s = seed["data_dir"], MockWhatsAppSender()
    res = flows.handle_text(d, s, REP_WA, "الخطة", "plan-2")
    assert res["status"] == "ok"
    assert "لا توجد خطة يومية" in s.texts_to("07000000011")[0]


def test_flow_checkin_checkout(seed):
    d, s = seed["data_dir"], MockWhatsAppSender()
    r1 = flows.handle_text(d, s, REP_WA, "حضور", "att-1")
    assert r1["status"] == "ok"
    assert "تم تسجيل الحضور" in s.texts_to("07000000011")[0]
    r2 = flows.handle_text(d, s, REP_WA, "انصراف", "att-2")
    assert r2["status"] == "ok"
    assert "تم تسجيل الانصراف" in s.texts_to("07000000011")[1]
    # double check-in -> friendly error, no crash
    r3 = flows.handle_text(d, s, REP_WA, "حضور", "att-3")
    assert r3["status"] == "error"
    assert "تعذر التنفيذ" in s.texts_to("07000000011")[2]


def test_flow_leave_types_and_request(seed):
    from app.services import leave
    d, s = seed["data_dir"], MockWhatsAppSender()
    rep, o1 = seed["rep1_o1"], seed["office1"]
    lt = leave.create_leave_type(d, seed["manager1"], office_id=o1,
                                 name="سنوية", default_days_per_year=21)
    r1 = flows.handle_text(d, s, REP_WA, "إجازة", "lv-1")
    assert r1["status"] == "ok"
    assert "سنوية" in s.texts_to("07000000011")[0]
    r2 = flows.handle_text(d, s, REP_WA,
                           f"إجازة {lt['id']} 2026-11-01 2026-11-03",
                           "lv-2")
    assert r2["status"] == "ok"
    assert "تم استلام طلب الإجازة" in s.texts_to("07000000011")[1]


def test_flow_help_on_unknown_command(seed):
    d, s = seed["data_dir"], MockWhatsAppSender()
    res = flows.handle_text(d, s, REP_WA, "مرحبا", "help-1")
    assert res["status"] == "ok"
    assert "الأوامر المتاحة" in s.texts_to("07000000011")[0]


# --- real sender never touched -------------------------------------------------

def test_cloud_sender_requires_configuration():
    with pytest.raises(ValueError):
        CloudApiWhatsAppSender(WhatsAppSettings())
