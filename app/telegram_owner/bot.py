"""Owner bot wiring and entry point.

Startup is fail-safe: without TELEGRAM_OWNER_IDS / TELEGRAM_OWNER_BOT_TOKEN
the process exits with a clear error instead of running open.
"""
from __future__ import annotations

import logging
from pathlib import Path

from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, filters)

from app.config import Settings
from app.jobs import runner as job_runner
from app.telegram_owner import auth, handlers

logger = logging.getLogger("owner_bot")


def build_application(settings: Settings, data_dir: Path) -> Application:
    auth.assert_owner_configured(settings.owner_ids, settings.telegram_owner_bot_token)
    app = (Application.builder()
           .token(settings.telegram_owner_bot_token)
           .build())
    app.bot_data["settings"] = settings
    app.bot_data["data_dir"] = data_dir
    app.add_handler(CommandHandler("start", handlers.start))
    app.add_handler(CommandHandler("menu", handlers.menu_cmd))
    app.add_handler(CallbackQueryHandler(handlers.on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.on_text))
    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    # Never log settings values (they contain the bot token).
    settings = Settings()
    data_dir = settings.resolved_data_dir
    try:
        app = build_application(settings, data_dir)
    except auth.BotNotConfiguredError as e:
        logger.error("refusing to start: %s", e)
        raise SystemExit(2)
    logger.info("owner bot starting (data_dir configured, polling)")
    # Scheduled jobs (reminders) tick alongside the bot. The runner is
    # idempotent: a second start returns the running scheduler instead of
    # double-ticking. Delivery is LoggingNotifier until real Telegram
    # sending is wired and tested — see app/jobs/runner.py.
    job_runner.start_runner(data_dir)
    try:
        app.run_polling(allowed_updates=["message", "callback_query"])
    finally:
        job_runner.stop_runner(data_dir)
