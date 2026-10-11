"""Owner authentication for the Telegram bot.

Rules:
- The ONLY source of truth for "who is owner" is TELEGRAM_OWNER_IDS from the
  environment (configured outside the bot) MATCHED against an active owner
  membership in the identity DB.
- /start NEVER grants owner. Unknown Telegram ids get a refusal + audit entry.
- If no owner ids are configured, the bot refuses to start (fail-safe).
"""
from __future__ import annotations

from pathlib import Path

from app.core.errors import PermissionDenied
from app.core.permissions import active_memberships
from app.databases import manager as dbm
from app.identity import models as im
from app.identity import repositories as repos


class BotNotConfiguredError(RuntimeError):
    """Raised at startup when owner identity is not configured."""


def assert_owner_configured(owner_ids: set[int], bot_token: str) -> None:
    """Fail-safe startup check. Raises BotNotConfiguredError if unusable."""
    if not owner_ids:
        raise BotNotConfiguredError(
            "TELEGRAM_OWNER_IDS is empty — refusing to start without an owner."
        )
    if not bot_token:
        raise BotNotConfiguredError(
            "TELEGRAM_OWNER_BOT_TOKEN is empty — refusing to start."
        )


def resolve_owner_actor(owner_ids: set[int], data_dir: Path, tg_id: int) -> int:
    """Return the identity user_id for a Telegram user id.

    Raises PermissionDenied for anyone who is not a configured owner with an
    active owner membership. Never creates or elevates anyone.
    """
    if tg_id not in owner_ids:
        _audit_denied(data_dir, tg_id, "telegram id not in owner list")
        raise PermissionDenied("not authorized")
    with dbm.identity_session(data_dir) as s:
        user = s.query(im.User).filter_by(telegram_id=tg_id).first()
        if user is None or user.status != "active":
            _audit_denied(data_dir, tg_id, "no active user for telegram id")
            raise PermissionDenied("not authorized")
        if not any(m.role == "owner" for m in active_memberships(s, user.id)):
            _audit_denied(data_dir, tg_id, "user has no active owner membership")
            raise PermissionDenied("not authorized")
        return user.id


def _audit_denied(data_dir: Path, tg_id: int, reason: str) -> None:
    """Best-effort audit of denied bot access. Never raises, never logs secrets."""
    try:
        with dbm.identity_session(data_dir) as s:
            repos.log_central_event(
                s, actor_user_id=None, event_type="BOT_ACCESS_DENIED",
                result="denied", reason=f"tg_id={tg_id}: {reason}",
            )
    except Exception:
        pass
