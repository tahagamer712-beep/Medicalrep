#!/usr/bin/env python3
"""Run the Owner Telegram bot.

Required environment (.env file, chmod 600):
    TELEGRAM_OWNER_IDS=123456789        # your Telegram user id (from @userinfobot)
    TELEGRAM_OWNER_BOT_TOKEN=...        # from @BotFather — never share it

Usage: python scripts/run_owner_bot.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.telegram_owner.bot import main

if __name__ == "__main__":
    main()
