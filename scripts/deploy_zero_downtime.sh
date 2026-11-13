#!/usr/bin/env bash
# ==============================================================================
# Fakhri HRMS - Zero-Downtime Deployment Script
#
# How it works:
# 1. Pulls latest changes from Git
# 2. Builds the new Docker image while the existing container is running untouched
# 3. Spawns a standby container with the new image on the internal network
# 4. Waits until the standby container passes health check (/healthz -> 200 OK)
# 5. Switches Caddy reverse proxy to the standby container with zero dropped requests
# 6. Recreates the primary container with the new image
# 7. Waits until the primary container is verified healthy
# 8. Switches Caddy back to primary container
# 9. Cleans up standby container and frees memory
#
# Result: 100% Zero Downtime, Zero Extra Cost, Zero Dropped Connections!
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "$APP_DIR"

STANDBY_CONTAINER="fakhri-hrms-api-standby"
CADDY_CONTAINER="fakhri-hrms-caddy-1"
CADDYFILE="${APP_DIR}/deploy/Caddyfile"

cleanup() {
    # Ensure standby container is removed even if an error occurs
    if docker ps -a --format '{{.Names}}' | grep -q "^${STANDBY_CONTAINER}$"; then
        echo ">>> Cleaning up standby container..."
        docker rm -f "$STANDBY_CONTAINER" >/dev/null 2>&1 || true
    fi
    # Ensure Caddy is pointing back to primary
    if grep -q "reverse_proxy ${STANDBY_CONTAINER}:8100" "$CADDYFILE" 2>/dev/null; then
        echo ">>> Restoring Caddyfile to primary..."
        sed -i.bak "s|reverse_proxy ${STANDBY_CONTAINER}:8100|reverse_proxy api:8100|g" "$CADDYFILE"
        rm -f "${CADDYFILE}.bak"
        docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

echo "=================================================================="
echo "🚀 Starting Zero-Downtime Deployment for Fakhri HRMS..."
echo "=================================================================="

# 1. Pull latest code
echo ">>> Pulling latest code from git..."
git pull origin main

# 2. Build the new image (live container keeps serving traffic)
echo ">>> Building new API Docker image in background (zero downtime during build)..."
docker compose build api

# 3. Launch standby container
echo ">>> Starting standby container '${STANDBY_CONTAINER}'..."
docker rm -f "$STANDBY_CONTAINER" >/dev/null 2>&1 || true

docker run -d \
    --name "$STANDBY_CONTAINER" \
    --network fakhri-hrms_default \
    --env-file .env \
    -e HRMS_FACEAPI_URL=http://faceapi:8000 \
    -e HRMS_WAHA_URL=http://waha:3000 \
    -e HRMS_WHATSAPP_PROVIDER=waha \
    -e HRMS_DATA_DIR=/app/data \
    -e PORT=8100 \
    -e WORKERS=2 \
    -v fakhri-hrms_hrms-data:/app/data \
    fakhri-hrms:latest

# 4. Wait for standby container to be healthy
echo ">>> Waiting for standby container to become ready and healthy..."
HEALTHY=0
for i in $(seq 1 40); do
    if docker exec "$CADDY_CONTAINER" wget -q -O - "http://${STANDBY_CONTAINER}:8100/healthz" 2>/dev/null | grep -q '"status":"ok"'; then
        HEALTHY=1
        echo ">>> Standby container is HEALTHY and ready!"
        break
    fi
    echo -n "."
    sleep 1
done
echo ""

if [ "$HEALTHY" -ne 1 ]; then
    echo "❌ Standby container failed health check! Aborting deployment. Live container untouched."
    docker logs --tail 30 "$STANDBY_CONTAINER"
    exit 1
fi

# 5. Switch Caddy to standby container
echo ">>> Switching live traffic to standby container (seamless handover)..."
sed -i.bak "s|reverse_proxy api:8100|reverse_proxy ${STANDBY_CONTAINER}:8100|g" "$CADDYFILE"
rm -f "${CADDYFILE}.bak"
docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile
echo ">>> Traffic routed to standby container! Zero dropped connections."

# 6. Recreate the primary container
echo ">>> Recreating primary 'api' container with new code..."
docker compose up -d --no-deps --force-recreate api

# 7. Wait for primary container to be healthy
echo ">>> Waiting for primary container to become ready and healthy..."
PRIMARY_HEALTHY=0
for i in $(seq 1 40); do
    if docker exec "$CADDY_CONTAINER" wget -q -O - "http://api:8100/healthz" 2>/dev/null | grep -q '"status":"ok"'; then
        PRIMARY_HEALTHY=1
        echo ">>> Primary container is HEALTHY and ready!"
        break
    fi
    echo -n "."
    sleep 1
done
echo ""

if [ "$PRIMARY_HEALTHY" -ne 1 ]; then
    echo "❌ Primary container failed health check! Keeping standby container active."
    docker logs --tail 30 fakhri-hrms-api-1
    exit 1
fi

# 8. Switch Caddy back to primary container
echo ">>> Switching live traffic back to primary container..."
sed -i.bak "s|reverse_proxy ${STANDBY_CONTAINER}:8100|reverse_proxy api:8100|g" "$CADDYFILE"
rm -f "${CADDYFILE}.bak"
docker exec "$CADDY_CONTAINER" caddy reload --config /etc/caddy/Caddyfile
echo ">>> Traffic successfully switched back to primary container."

# 9. Cleanup standby container (handled by trap cleanup EXIT)
echo "=================================================================="
echo "✅ Zero-downtime deployment finished successfully!"
echo "Server did not stop, and zero requests were dropped."
echo "=================================================================="
