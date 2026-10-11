#!/usr/bin/env bash
# تشغيل خادم الـ API (FastAPI) — يخدم /health و /whatsapp/webhook.
# الاستخدام: bash scripts/run_api.sh [PORT]
# ملاحظة: الـ webhook يحتاج عنوانًا عامًا (نطاق + HTTPS) عند ربطه مع Meta.
set -u
cd "$(dirname "$0")/.." || exit 1
PORT="${1:-8000}"

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="python3"
fi

if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
fi

echo "API on http://127.0.0.1:${PORT}  (health: /health, whatsapp: /whatsapp/webhook)"
exec $PY -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT"
