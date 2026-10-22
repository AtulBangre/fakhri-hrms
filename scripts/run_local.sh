#!/usr/bin/env bash
#
# Run the whole stack on THIS laptop for local testing.
#
#   bash scripts/run_local.sh                  # laptop only  → http://127.0.0.1:8100
#   HOST=0.0.0.0 bash scripts/run_local.sh     # + phone on the same Wi-Fi
#   TUNNEL=1 bash scripts/run_local.sh         # + real HTTPS URL (phone camera works)
#   HOST=0.0.0.0 TUNNEL=1 bash scripts/run_local.sh   # both, for full mobile testing
#
# Ctrl+C stops everything (Face API and the tunnel too).
#
# Why HTTPS matters for phones: browsers only hand over the camera on a "secure
# context" — https:// or localhost. A LAN address like http://192.168.1.5:8100 is
# NOT secure, so getUserMedia is blocked there. Two ways round it:
#   * TUNNEL=1  → cloudflared quick tunnel gives a real https:// URL (works on
#                 iOS and Android, no flags, no certificate install)
#   * or on Android only: chrome://flags → "Insecure origins treated as secure"
#                 → add  http://<laptop-ip>:8100  → relaunch Chrome
# HR work (adding employees, file-upload enrolment, payroll) works fine over plain
# LAN HTTP; only live camera capture needs HTTPS.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FACEAPI_DIR="${FACEAPI_DIR:-$ROOT/../faceapi}"
CLOUDFLARED="${CLOUDFLARED:-$ROOT/../tools/cloudflared}"

HOST="${HOST:-127.0.0.1}"          # use 0.0.0.0 to let phones on the Wi-Fi connect
TUNNEL="${TUNNEL:-0}"              # 1 = start a cloudflared HTTPS tunnel
FACEAPI_PORT="${FACEAPI_PORT:-8000}"
HRMS_PORT="${HRMS_PORT:-8100}"
HR_PASSWORD="${HR_PASSWORD:-fakhri-hr-2026}"
GATE_PASSWORD="${GATE_PASSWORD:-fakhri-gate-2026}"

if [ ! -x "$ROOT/.venv/bin/python3" ]; then
  echo "error: HRMS virtualenv missing. Run: bash scripts/setup_local.sh" >&2
  exit 1
fi
if [ ! -x "$FACEAPI_DIR/.venv/bin/python3" ]; then
  echo "error: Face API virtualenv missing at $FACEAPI_DIR" >&2
  echo "       Fix it with:  cd $FACEAPI_DIR && bash scripts/setup_local.sh" >&2
  exit 1
fi

# ------------------------------------------------------------------ helpers
port_busy() { lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; }
lan_ip() { ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo ""; }

wait_for() {  # <url> <seconds> <label>
  for _ in $(seq 1 "$2"); do
    curl -sf --max-time 2 "$1" >/dev/null 2>&1 && return 0
    sleep 1
  done
  echo "warning: $3 did not answer at $1 in time" >&2
  return 1
}

FACEAPI_PID=""
TUNNEL_PID=""
cleanup() {
  if [ -n "$TUNNEL_PID" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
    echo "==> closing the HTTPS tunnel"
    kill "$TUNNEL_PID" 2>/dev/null || true
  fi
  if [ -n "$FACEAPI_PID" ] && kill -0 "$FACEAPI_PID" 2>/dev/null; then
    echo "==> stopping the Face API (pid $FACEAPI_PID)"
    kill "$FACEAPI_PID" 2>/dev/null || true
    wait "$FACEAPI_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

# --------------------------------------------------------------- Face API
if port_busy "$FACEAPI_PORT"; then
  echo "==> Face API already running on port $FACEAPI_PORT (reusing it)"
else
  echo "==> starting Face API on http://127.0.0.1:$FACEAPI_PORT"
  (
    cd "$FACEAPI_DIR"
    FACEAPI_AUTH_ENABLED=false \
      exec ./.venv/bin/python3 cli.py serve --host 127.0.0.1 --port "$FACEAPI_PORT" --workers 1
  ) >"$ROOT/data-faceapi.log" 2>&1 &
  FACEAPI_PID=$!
  wait_for "http://127.0.0.1:$FACEAPI_PORT/readyz" 45 "Face API" || {
    echo "    see $ROOT/data-faceapi.log" >&2
  }
fi

# -------------------------------------------------------------------- HRMS
if port_busy "$HRMS_PORT"; then
  echo "error: port $HRMS_PORT is already in use — stop that process first" >&2
  exit 1
fi

if [ ! -f "$ROOT/data/hrms.db" ]; then
  echo "==> first run: creating the database, logins and demo data"
  (
    cd "$ROOT"
    HRMS_DEFAULT_HR_PASSWORD="$HR_PASSWORD" HRMS_DEFAULT_KIOSK_PASSWORD="$GATE_PASSWORD" \
      ./.venv/bin/python3 cli.py init
    ./.venv/bin/python3 cli.py seed-demo --employees 12
  )
fi

# ------------------------------------------------------------------ tunnel
TUNNEL_URL=""
if [ "$TUNNEL" = "1" ]; then
  if [ ! -x "$CLOUDFLARED" ]; then
    echo "warning: cloudflared not found at $CLOUDFLARED — skipping the HTTPS tunnel" >&2
  else
    echo "==> opening an HTTPS tunnel to 127.0.0.1:$HRMS_PORT"
    TUNNEL_LOG="$ROOT/data-tunnel.log"
    "$CLOUDFLARED" tunnel --no-autoupdate --url "http://127.0.0.1:$HRMS_PORT" >"$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!
    for _ in $(seq 1 40); do
      TUNNEL_URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" 2>/dev/null | head -1 || true)"
      [ -n "$TUNNEL_URL" ] && break
      sleep 1
    done
    if [ -n "$TUNNEL_URL" ]; then
      wait_for "$TUNNEL_URL/healthz" 30 "public tunnel" || true
    else
      echo "warning: could not read the tunnel URL — see $TUNNEL_LOG" >&2
    fi
  fi
fi

# ------------------------------------------------------------------ banner
IP="$(lan_ip)"
cat <<BANNER

  ┌────────────────────────────────────────────────────────────────────────────┐
  │  Fakhri HRMS — running on this laptop                                      │
  ├────────────────────────────────────────────────────────────────────────────┤
  │  HR console    http://127.0.0.1:$HRMS_PORT/dashboard
  │                login: hr    / $HR_PASSWORD
  │                                                                            │
  │  Gate kiosk    http://127.0.0.1:$HRMS_PORT/kiosk
  │                login: gate  / $GATE_PASSWORD
  │                                                                            │
  │  Face API      http://127.0.0.1:$FACEAPI_PORT/docs
  │  Health        http://127.0.0.1:$HRMS_PORT/api/health
BANNER

if [ "$HOST" = "0.0.0.0" ]; then
  MDNS_NAME="$(scutil --get LocalHostName 2>/dev/null || hostname -s).local"
cat <<BANNER
  ├────────────────────────────────────────────────────────────────────────────┤
  │  FROM YOUR PHONE (same Wi-Fi)                                              │
BANNER
  if [ -n "$IP" ]; then
cat <<BANNER
  │    Gate kiosk  http://$IP:$HRMS_PORT/kiosk
  │    HR console  http://$IP:$HRMS_PORT/dashboard
BANNER
  fi
cat <<BANNER
  │    (also)      http://$MDNS_NAME:$HRMS_PORT/kiosk
  │                ^ this name survives an IP change on iPhone/iPad; on Android
  │                  use the numeric address above
  │                                                                            
  │    note: camera capture stays blocked on plain http — use TUNNEL=1 below,
  │          or upload a photo instead of capturing one
BANNER
fi

if [ -n "$TUNNEL_URL" ]; then
cat <<BANNER
  ├────────────────────────────────────────────────────────────────────────────┤
  │  HTTPS (camera works on phones)                                            │
  │    $TUNNEL_URL/dashboard
  │    $TUNNEL_URL/kiosk
  │    ! this URL is public on the internet — testing only, and change the
  │      passwords first if you plan to leave it open
BANNER
fi

cat <<BANNER
  └────────────────────────────────────────────────────────────────────────────┘

  Ctrl+C stops everything (HRMS, Face API and the tunnel).

BANNER

cd "$ROOT"
HRMS_DEFAULT_HR_PASSWORD="$HR_PASSWORD" HRMS_DEFAULT_KIOSK_PASSWORD="$GATE_PASSWORD" \
  ./.venv/bin/python3 cli.py serve --host "$HOST" --port "$HRMS_PORT" --workers 1
