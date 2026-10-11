"""WhatsApp Cloud API webhook (FastAPI router).

GET  /whatsapp/webhook — verification handshake during Meta dashboard setup.
POST /whatsapp/webhook — inbound events. The X-Hub-Signature-256 header is
verified against WHATSAPP_APP_SECRET with a constant-time comparison;
a bad or missing signature -> 403 and the payload is never processed.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from app.config import Settings
from app.whatsapp import flows
from app.whatsapp.config import whatsapp_settings
from app.whatsapp.sender import MockWhatsAppSender, WhatsAppSender

logger = logging.getLogger("whatsapp_webhook")

router = APIRouter(prefix="/whatsapp", tags=["whatsapp"])

# Sender used by the webhook. Tests inject MockWhatsAppSender; production
# wiring (CloudApiWhatsAppSender) happens at deployment, never in tests.
_sender: WhatsAppSender = MockWhatsAppSender()


def set_sender(sender: WhatsAppSender) -> None:
    global _sender
    _sender = sender


def _check_signature(raw: bytes, signature: str | None) -> None:
    secret = whatsapp_settings.app_secret.encode()
    if not secret or not signature or not signature.startswith("sha256="):
        raise HTTPException(status_code=403, detail="missing signature")
    expected = hmac.new(secret, raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature[len("sha256="):]):
        raise HTTPException(status_code=403, detail="bad signature")


@router.get("/webhook")
def verify(hub_mode: str = Query("", alias="hub.mode"),
           hub_verify_token: str = Query("", alias="hub.verify_token"),
           hub_challenge: str = Query("", alias="hub.challenge")) -> Response:
    """Meta verification handshake: echo the challenge when the token matches."""
    if (hub_mode == "subscribe"
            and hub_verify_token
            and hmac.compare_digest(hub_verify_token,
                                    whatsapp_settings.verify_token)):
        return Response(content=hub_challenge, media_type="text/plain")
    raise HTTPException(status_code=403, detail="verification failed")


@router.post("/webhook")
async def inbound(request: Request,
                  x_hub_signature_256: str | None = Header(
                      default=None, alias="X-Hub-Signature-256")) -> dict:
    raw = await request.body()
    _check_signature(raw, x_hub_signature_256)
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON")
    # Fresh data_dir per request: MEDREP_DATA_DIR may change between
    # processes/tests; never use the import-time singleton here.
    import os as _os
    data_dir = Path(_os.environ.get("MEDREP_DATA_DIR")
                    or Settings().resolved_data_dir)
    results: list[dict] = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            for msg in value.get("messages", []):
                if msg.get("type") != "text":
                    continue  # text flows only for now
                results.append(flows.handle_text(
                    data_dir, _sender,
                    wa_phone=msg.get("from", ""),
                    text=(msg.get("text") or {}).get("body", ""),
                    waba_message_id=msg.get("id", ""),
                ))
    return {"status": "ok", "processed": len(results)}
