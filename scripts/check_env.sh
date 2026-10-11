#!/usr/bin/env bash
# فحص بيئة التشغيل: يعمل على Termux وLinux العادي.
# الاستخدام: bash scripts/check_env.sh
# رمز الخروج: 0 = صالح للبدء، 1 = توجد عوائق حرجة.

set -u
FAIL=0
WARN=0

ok()   { echo "[OK]   $1"; }
warn() { echo "[WARN] $1"; WARN=$((WARN+1)); }
fail() { echo "[FAIL] $1"; FAIL=$((FAIL+1)); }

echo "=== Medical Rep System — Environment Check ==="
echo "Date (UTC): $(date -u '+%Y-%m-%d %H:%M:%S')"
echo

# --- Termux detection ---
if [ -n "${TERMUX_VERSION:-}" ] || [ -d "/data/data/com.termux" ]; then
    IS_TERMUX=1
    ok "Running on Termux (version: ${TERMUX_VERSION:-unknown})"
else
    IS_TERMUX=0
    warn "Not Termux — generic Linux (dev machine). Phone checks skipped."
fi

# --- OS / arch ---
echo "OS: $(uname -srm)"

# --- Python >= 3.11 ---
if command -v python3 >/dev/null 2>&1; then
    PYV=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')
    PY_OK=$(python3 -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')
    if [ "$PY_OK" = "1" ]; then
        ok "Python $PYV (>= 3.11)"
    else
        fail "Python $PYV too old — need >= 3.11"
    fi
else
    fail "python3 not found"
fi

# --- pip + venv ---
if python3 -m pip --version >/dev/null 2>&1; then
    ok "pip available: $(python3 -m pip --version | cut -d' ' -f2)"
else
    fail "pip not available (pkg install python-pip / ensurepip)"
fi
if python3 -m venv --help >/dev/null 2>&1; then
    ok "venv module available"
else
    fail "venv module missing"
fi

# --- sqlite3 ---
SQLV=$(python3 -c 'import sqlite3; print(sqlite3.sqlite_version)' 2>/dev/null || echo "missing")
if [ "$SQLV" != "missing" ]; then
    ok "SQLite $SQLV (WAL mode will be enabled per-database)"
else
    fail "sqlite3 Python module missing"
fi

# --- Disk space (need >= 2GB free for data+backups) ---
if command -v df >/dev/null 2>&1; then
    FREE_KB=$(df -k . | tail -1 | awk '{print $4}')
    FREE_GB=$((FREE_KB / 1024 / 1024))
    if [ "$FREE_GB" -ge 2 ]; then
        ok "Free disk: ${FREE_GB}GB (>= 2GB)"
    else
        fail "Free disk only ${FREE_GB}GB — need >= 2GB"
    fi
fi

# --- Memory ---
if [ -r /proc/meminfo ]; then
    MEM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
    if [ "$MEM_MB" -ge 1500 ]; then
        ok "RAM: ${MEM_MB}MB"
    else
        warn "RAM only ${MEM_MB}MB — workable but tight for +20 concurrent users"
    fi
fi

# --- Network ---
if command -v ping >/dev/null 2>&1; then
    if ping -c1 -W3 8.8.8.8 >/dev/null 2>&1; then
        ok "Internet reachable (needed for Telegram bot + pip)"
    else
        warn "No internet right now — Telegram bot and pip installs need it"
    fi
else
    warn "ping not available — skipping network check"
fi

# --- Termux-specific ---
if [ "$IS_TERMUX" = "1" ]; then
    if command -v termux-wake-lock >/dev/null 2>&1; then
        ok "termux-wake-lock available (use it before starting services)"
    else
        warn "termux-wake-lock missing — install termux-api pkg for stable background run"
    fi
    if [ -d "$HOME/.termux" ]; then
        ok "Termux home OK: $HOME"
    fi
    warn "REMINDER: disable battery optimization for Termux in Android Settings,"
    warn "and re-run scripts/start.sh manually after every phone reboot (until auto-start is tested)."
fi

# --- Writable data dir ---
if mkdir -p ./data 2>/dev/null && [ -w ./data ]; then
    ok "data/ directory writable"
else
    fail "cannot write ./data — check permissions"
fi

echo
echo "=== Summary: FAIL=$FAIL WARN=$WARN ==="
if [ "$FAIL" -gt 0 ]; then
    echo "Result: BLOCKED — fix FAIL items before Phase 2."
    exit 1
fi
echo "Result: READY — environment is suitable for Phase 2."
exit 0
