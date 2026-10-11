"""Real Telegram delivery for scheduled reminders.

Separation of concerns (explicit, by design):
- PRODUCTION: TelegramNotifier — sends via the Bot API. Enabled only when
  REMINDER_DELIVERY=telegram AND a bot token is configured.
- DEFAULT/TESTS: LoggingNotifier (runner.py) / FakeNotifier (tests).

Nothing here sends during tests: this module is only instantiated when the
operator explicitly opts into real delivery. We never claim delivery unless
the Bot API confirms it (no exception).
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.databases import manager as dbm
from app.identity import models as im

logger = logging.getLogger("telegram_notifier")


class TelegramNotifier:
    """Notifier protocol implementation over python-telegram-bot.

    Maps the internal user_id to the user's telegram_id from the identity
    DB. Returns True only if the Bot API accepted the message.
    """

    def __init__(self, data_dir: Path, bot_token: str):
        self._data_dir = data_dir
        self._bot_token = bot_token
        self._bot = None

    def _get_bot(self):
        if self._bot is None:
            from telegram import Bot
            # Never log the token.
            self._bot = Bot(token=self._bot_token)
        return self._bot

    def _telegram_id(self, user_id: int) -> int | None:
        with dbm.identity_session(self._data_dir) as s:
            u = s.get(im.User, user_id)
            return u.telegram_id if u and u.telegram_id else None

    def send(self, user_id: int, text: str) -> bool:
        chat_id = self._telegram_id(user_id)
        if not chat_id:
            logger.warning("no telegram_id for user %s — reminder not sent",
                           user_id)
            return False
        try:
            import asyncio
            asyncio.run(self._get_bot().send_message(chat_id=chat_id,
                                                     text=text))
        except Exception as e:  # network/API errors -> retry path
            logger.warning("telegram send failed for user %s: %s", user_id,
                           type(e).__name__)
            return False
        return True


def build_default_notifier(data_dir: Path):
    """Choose the notifier from configuration. Defaults to logging
    (no real sends) unless the operator explicitly enables Telegram."""
    from app.config import Settings
    from app.jobs.runner import LoggingNotifier
    settings = Settings()
    if (settings.reminder_delivery == "telegram"
            and settings.telegram_owner_bot_token):
        logger.info("reminder delivery: Telegram (real sends enabled)")
        return TelegramNotifier(data_dir, settings.telegram_owner_bot_token)
    logger.info("reminder delivery: log-only (no real sends)")
    return LoggingNotifier()
