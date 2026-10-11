#!/usr/bin/env bash
# حالة بوت المالك + آخر السجلات.
# الاستخدام: bash scripts/status.sh
set -u
cd "$(dirname "$0")/.." || exit 1

PIDFILE="logs/bot.pid"
LOGFILE="logs/bot.log"
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "[OK] running (pid $(cat "$PIDFILE"))"
else
    echo "[STOPPED] bot is not running. Start with: bash scripts/start.sh"
fi
if [ -f "$LOGFILE" ]; then
    echo "--- last 15 log lines ---"
    tail -n 15 "$LOGFILE"
fi
