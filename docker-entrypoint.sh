#!/bin/sh
# Container entrypoint: env vars → uvicorn flags, plus a first-run notice.
set -eu

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8100}"
WORKERS="${WORKERS:-1}"
LOG_LEVEL="${LOG_LEVEL:-info}"
# Past this many open connections uvicorn answers 503 at once instead of queueing
# without limit (a backlog of scans is what made the app look dead).
LIMIT_CONCURRENCY="${LIMIT_CONCURRENCY:-200}"
# Python's allocator keeps one memory arena per thread; with ~100 request threads that
# inflated resident memory towards the container limit. Two arenas is plenty here.
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"

FORWARDED_ALLOW_IPS="${FORWARDED_ALLOW_IPS:-127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16}"

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
  --limit-concurrency "$LIMIT_CONCURRENCY" \
  --timeout-keep-alive 15 \
  --proxy-headers \
  --forwarded-allow-ips "$FORWARDED_ALLOW_IPS"
