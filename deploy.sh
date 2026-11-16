#!/bin/bash
# Deploy na VPS: pull → build → up -d → čekaj da web kontejner bude healthy.
# Build ide PRIJE zamjene kontejnera i nema `down` — ako pull ili build padnu,
# stara verzija ostaje u pogonu.
set -eu

APP_DIR="${DEPLOY_DIR:-/programi/hep_ha}"
WEB_CONTAINER="${DEPLOY_WEB_CONTAINER:-hep-energy-web}"
HEALTH_TIMEOUT="${DEPLOY_HEALTH_TIMEOUT:-120}"   # sekundi

log() { echo "[$(date)] $*"; }

cd "$APP_DIR"

# `docker compose` (plugin v2), fallback na stari `docker-compose`
if docker compose version >/dev/null 2>&1; then
    COMPOSE="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE="docker-compose"
else
    log "GRESKA: nema ni 'docker compose' ni 'docker-compose'" >&2
    exit 1
fi

log "Pulling from GitHub..."
git pull origin main

log "Building image ($COMPOSE)..."
$COMPOSE build

log "Starting containers..."
$COMPOSE up -d

log "Cekam da $WEB_CONTAINER bude healthy (max ${HEALTH_TIMEOUT}s)..."
WAITED=0
STATUS=unknown
while [ "$WAITED" -lt "$HEALTH_TIMEOUT" ]; do
    STATUS=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
             "$WEB_CONTAINER" 2>/dev/null || echo unknown)
    if [ "$STATUS" = "healthy" ]; then
        log "Deploy gotov! ($WEB_CONTAINER healthy nakon ${WAITED}s)"
        exit 0
    fi
    sleep "${DEPLOY_POLL_SEC:-5}"
    WAITED=$((WAITED + ${DEPLOY_POLL_SEC:-5}))
done

log "DEPLOY NEUSPJESAN: $WEB_CONTAINER nije healthy nakon ${HEALTH_TIMEOUT}s (status: $STATUS)" >&2
log "Provjeri: docker logs --tail 50 $WEB_CONTAINER" >&2
exit 1
