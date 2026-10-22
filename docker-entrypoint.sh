#!/bin/sh
# Container entrypoint: env vars → uvicorn flags, plus a first-run notice.
set -eu

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8100}"
WORKERS="${WORKERS:-1}"
LOG_LEVEL="${LOG_LEVEL:-info}"

# First run creates the database, the HR login and the kiosk login. Print them
# once so the operator can pick them up from `docker compose logs`.
if [ ! -f "${HRMS_DATA_DIR:-/app/data}/hrms.db" ]; then
  echo "entrypoint: first run — initialising database and logins"
  python cli.py init || true
fi

if [ -n "${HRMS_FACEAPI_URL:-}" ]; then
  echo "entrypoint: face service expected at ${HRMS_FACEAPI_URL}"
fi

echo "entrypoint: starting HRMS on ${HOST}:${PORT} with ${WORKERS} worker(s)"
exec uvicorn app.main:app \
  --host "$HOST" \
  --port "$PORT" \
  --workers "$WORKERS" \
  --log-level "$LOG_LEVEL" \
  --proxy-headers \
  --forwarded-allow-ips '*'
