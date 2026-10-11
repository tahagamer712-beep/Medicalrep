"""Approved template registry + 24h conversation-window policy.

Meta rule: a business may send free-form messages only within 24 hours of
the user's last inbound message. Outside the window, only pre-approved
templates may be used.

Templates must be created and approved in the Meta WhatsApp dashboard
first; this registry just names the ones our flows are allowed to use.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from app.whatsapp.sender import WhatsAppSender

WINDOW = timedelta(hours=24)

# name -> {"language": ..., "params": [descriptions for the dashboard]}
TEMPLATES: dict[str, dict] = {
    "daily_plan": {
        "language": "ar",
        "params": ["plan_date", "summary"],
        "description": "يرسل ملخص الخطة اليومية عند الطلب خارج نافذة المحادثة",
    },
    "attendance_confirm": {
        "language": "ar",
        "params": ["action", "time"],
        "description": "تأكيد تسجيل الحضور/الانصراف خارج نافذة المحادثة",
    },
    "leave_received": {
        "language": "ar",
        "params": ["request_id", "days"],
        "description": "تأكيد استلام طلب الإجازة خارج نافذة المحادثة",
    },
}


def within_window(last_inbound_at: datetime | None,
                  now: datetime) -> bool:
    """True if free-form text is allowed (user wrote within 24h)."""
    if last_inbound_at is None:
        return False
    start = last_inbound_at.replace(tzinfo=None) \
        if last_inbound_at.tzinfo else last_inbound_at
    end = now.replace(tzinfo=None) if now.tzinfo else now
    return (end - start) <= WINDOW


def send_smart(sender: WhatsAppSender, to: str, text: str, *,
               template: str, template_params: list[str],
               last_inbound_at: datetime | None,
               now: datetime) -> tuple[str, str]:
    """Send free text inside the window, else the approved template.

    Returns (kind, provider_message_id) where kind is "text"|"template".
    Raises KeyError for an unknown template name (fail fast, never send
    an unregistered template name to Meta).
    """
    if within_window(last_inbound_at, now):
        return "text", sender.send_text(to, text)
    if template not in TEMPLATES:
        raise KeyError(f"unknown WhatsApp template: {template!r}")
    return "template", sender.send_template(
        to, template, template_params,
        language=TEMPLATES[template]["language"])
