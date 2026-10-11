"""WhatsApp message sending.

- WhatsAppSender: protocol both implementations satisfy.
- MockWhatsAppSender: records messages in memory for tests. No network.
- CloudApiWhatsAppSender: real Cloud API calls via requests.
  NEVER instantiated or called in tests — no test may touch the network
  or a real Meta account.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from app.whatsapp.config import WhatsAppSettings


@dataclass
class SentMessage:
    kind: str  # "text" | "template"
    to: str
    body: str = ""
    template: str = ""
    template_params: list[str] = field(default_factory=list)
    provider_message_id: str = ""


class WhatsAppSender(Protocol):
    def send_text(self, to: str, text: str) -> str:
        """Send a free-form text (only inside the 24h window)."""
        ...

    def send_template(self, to: str, template: str,
                      params: list[str], language: str = "ar") -> str:
        """Send an approved template (required outside the 24h window)."""
        ...


class MockWhatsAppSender:
    """In-memory sender for tests."""

    def __init__(self) -> None:
        self.sent: list[SentMessage] = []
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"mock-waba-{self._counter}"

    def send_text(self, to: str, text: str) -> str:
        mid = self._next_id()
        self.sent.append(SentMessage(kind="text", to=to, body=text,
                                     provider_message_id=mid))
        return mid

    def send_template(self, to: str, template: str,
                      params: list[str], language: str = "ar") -> str:
        mid = self._next_id()
        self.sent.append(SentMessage(kind="template", to=to, template=template,
                                     template_params=list(params),
                                     provider_message_id=mid))
        return mid

    def texts_to(self, to: str) -> list[str]:
        return [m.body for m in self.sent
                if m.kind == "text" and m.to == to]

    def templates_to(self, to: str) -> list[str]:
        return [m.template for m in self.sent
                if m.kind == "template" and m.to == to]


class CloudApiWhatsAppSender:
    """Real Cloud API sender. Production only — never in tests."""

    def __init__(self, settings: WhatsAppSettings) -> None:
        if not settings.is_configured():
            raise ValueError("WhatsApp is not configured: missing credentials")
        self._s = settings

    def _post(self, payload: dict) -> str:
        import requests  # local import: only this class needs it
        url = (f"https://graph.facebook.com/{self._s.api_version}/"
               f"{self._s.phone_number_id}/messages")
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {self._s.access_token}",
                     "Content-Type": "application/json"},
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        msgs = data.get("messages") or []
        if not msgs or "id" not in msgs[0]:
            raise ValueError(f"unexpected Cloud API response: {data!r}"[:200])
        return str(msgs[0]["id"])

    def send_text(self, to: str, text: str) -> str:
        return self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text, "preview_url": False},
        })

    def send_template(self, to: str, template: str,
                      params: list[str], language: str = "ar") -> str:
        components = []
        if params:
            components = [{
                "type": "body",
                "parameters": [{"type": "text", "text": p} for p in params],
            }]
        return self._post({
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {"name": template,
                         "language": {"code": language},
                         "components": components},
        })
