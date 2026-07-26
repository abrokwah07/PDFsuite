#!/usr/bin/env bash
# Local development launcher for Local PDF Suite
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -d .venv ]]; then
  python3 -m venv .venv
  # shellcheck disable=SC1091
  source .venv/bin/activate
  pip install -r requirements.txt
else
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

export APP_ENV="${APP_ENV:-development}"
export HOST="${HOST:-127.0.0.1}"
export PORT="${PORT:-8000}"

# Load .env if present (optional)
if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "Starting Local PDF Suite on http://${HOST}:${PORT} (APP_ENV=${APP_ENV})"
exec uvicorn main:app --host "${HOST}" --port "${PORT}" --reload --proxy-headers --forwarded-allow-ips="${FORWARDED_ALLOW_IPS:-127.0.0.1}"
