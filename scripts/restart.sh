#!/usr/bin/env bash
# إعادة تشغيل بوت المالك + المجدول (آمنة: حالة المهام في قاعدة البيانات، والمجدول مكرر-آمن).
# الاستخدام: bash scripts/restart.sh
set -u
cd "$(dirname "$0")/.." || exit 1
bash scripts/stop.sh
sleep 1
bash scripts/start.sh
