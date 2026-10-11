#!/usr/bin/env bash
# إيقاف بوت المالك + المجدول.
# الاستخدام: bash scripts/stop.sh
set -u
cd "$(dirname "$0")/.." || exit 1

PIDFILE="logs/bot.pid"
if [ ! -f "$PIDFILE" ]; then
    echo "[OK] not running (no pid file)."
    exit 0
fi
PID=$(cat "$PIDFILE")
if kill -0 "$PID" 2>/dev/null; then
    kill "$PID"
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        kill -0 "$PID" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "$PID" 2>/dev/null; then
        kill -9 "$PID"
        echo "[WARN] force-killed pid $PID"
    else
        echo "[OK] stopped pid $PID"
    fi
else
    echo "[OK] pid $PID already gone."
fi
rm -f "$PIDFILE"
