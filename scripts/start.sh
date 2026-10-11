#!/usr/bin/env bash
# تشغيل بوت المالك + المجدول في الخلفية. يعمل على Termux وLinux.
# الاستخدام: bash scripts/start.sh
set -u
cd "$(dirname "$0")/.." || exit 1

PIDFILE="logs/bot.pid"
LOGFILE="logs/bot.log"
mkdir -p logs

# --- .env ---
if [ ! -f .env ]; then
    cp .env.example .env
    chmod 600 .env
    echo "[WARN] created .env from .env.example — fill TELEGRAM_OWNER_IDS and TELEGRAM_OWNER_BOT_TOKEN, then re-run."
fi

# --- token present? (presence only, never printed) ---
if ! grep -qE '^TELEGRAM_OWNER_BOT_TOKEN=<redacted>[^[:space:]#]+' .env 2>/dev/null; then
    echo "[FAIL] TELEGRAM_OWNER_BOT_TOKEN=<redacted> is empty in .env — put the bot token there (chmod 600) and re-run."
    exit 1
fi
if ! grep -qE '^TELEGRAM_OWNER_IDS=[0-9]' .env 2>/dev/null; then
    echo "[FAIL] TELEGRAM_OWNER_IDS is empty in .env — put your Telegram numeric id (from @userinfobot) and re-run."
    exit 1
fi

# --- already running? ---
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "[OK] already running (pid $(cat "$PIDFILE")). Use scripts/restart.sh to restart."
    exit 0
fi

# --- python ---
if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

# --- Termux: keep the CPU awake ---
if command -v termux-wake-lock >/dev/null 2>&1; then
    termux-wake-lock
    echo "[OK] termux-wake-lock acquired"
fi

# --- start ---
# shellcheck disable=SC2086
nohup $PY scripts/run_owner_bot.py >>"$LOGFILE" 2>&1 &
echo $! > "$PIDFILE"
sleep 2
if kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "[OK] bot started (pid $(cat "$PIDFILE")). Log: $LOGFILE"
    echo "     reminders mode: $(grep -E '^REMINDER_DELIVERY=' .env 2>/dev/null || echo 'REMINDER_DELIVERY=log (default, safe: no real sending)')"
else
    echo "[FAIL] bot died immediately — check $LOGFILE"
    rm -f "$PIDFILE"
    exit 1
fi
