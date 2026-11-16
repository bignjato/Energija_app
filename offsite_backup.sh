#!/bin/sh
# Off-site backup zadnjeg lokalnog backup-a baze.
#
# Konfiguracija preko env-a (postavi u docker-compose / .env):
#   OFFSITE_BACKUP_MODE      = rclone | rsync | disabled  (default disabled)
#   OFFSITE_BACKUP_DEST      = npr. "myremote:hep-backups/"   (rclone)
#                              ili "user@host:/path/"          (rsync over SSH)
#   OFFSITE_BACKUP_KEEP      = koliko file-ova zadržati remote  (default 14)
#
# Za rclone: konfiguraciju (~/.config/rclone/rclone.conf) montiraj u kontejner
#   ili stavi RCLONE_CONFIG_<REMOTE>_* env varijable.
# Za rsync: stavi SSH ključ na poznatu lokaciju i mountiraj read-only.

set -e

# SYNC_DATA_DIR override postoji zbog testova; default = produkcija (/data)
DATA_DIR="${SYNC_DATA_DIR:-/data}"
BACKUP_DIR="$DATA_DIR/backups"
LOG="$DATA_DIR/sync.log"
MARKER="$DATA_DIR/.last_offsite_ok"
MODE="${OFFSITE_BACKUP_MODE:-disabled}"
DEST="${OFFSITE_BACKUP_DEST:-}"
KEEP="${OFFSITE_BACKUP_KEEP:-14}"
# Limiti (s). sync_loop.sh cijeloj skripti daje T_OFFSITE=900 s, pa ZBROJ
# upload + retencija (svaki + KILL_SEC do SIGKILL-a) mora ostati ispod toga:
# 660+30 + 120+30 = 840. Inače vanjski timeout ubije skriptu usred retencije
# (lažni "[offsite] GRESKA exit=124" nakon uspjelog uploada), a unutarnji
# timeout + rclone ostanu visjeti jer su u vlastitoj process grupi.
MAX_SEC="${OFFSITE_TIMEOUT:-660}"
RETENTION_SEC="${OFFSITE_RETENTION_TIMEOUT:-120}"
KILL_SEC=30

log() {
    printf '[%s] [offsite] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOG"
}

# run_logged SEKUNDE naredba... — pokreni naredbu (uz coreutils timeout ako
# postoji), output u log, a vrati status SAME naredbe. `cmd | tee` bi vratio status tee-a, pa bi se i neuspjeli
# upload vodio kao OK.
run_logged() {
    _rc=0
    _limit=$1; shift
    if command -v timeout >/dev/null 2>&1; then
        _out=$(timeout -k "$KILL_SEC" "$_limit" "$@" 2>&1) || _rc=$?
    else
        _out=$("$@" 2>&1) || _rc=$?
    fi
    if [ -n "$_out" ]; then
        printf '%s\n' "$_out" | tee -a "$LOG"
    fi
    return "$_rc"
}

if [ "$MODE" = "disabled" ] || [ -z "$DEST" ]; then
    log "off-site backup onemogućen (MODE=$MODE, DEST=$DEST)"
    exit 0
fi

LATEST=$(ls -t "$BACKUP_DIR"/hep_energy_*.db 2>/dev/null | head -n1)
if [ -z "$LATEST" ]; then
    log "WARN: nema lokalnog backup-a u $BACKUP_DIR"
    exit 1
fi

case "$MODE" in
    rclone)
        if ! command -v rclone >/dev/null 2>&1; then
            log "ERROR: rclone nije instaliran u image-u"
            exit 2
        fi
        log "rclone copy $LATEST → $DEST"
        if run_logged "$MAX_SEC" rclone copy --contimeout 30s --timeout 5m "$LATEST" "$DEST"; then
            log "OK rclone upload"
            date '+%Y-%m-%d' > "$MARKER"
            # Retencija na remote — kratak vlastiti limit (vidi zbroj gore)
            run_logged "$RETENTION_SEC" rclone delete --contimeout 30s --timeout 1m \
                --min-age "${KEEP}d" "$DEST" || log "WARN: rclone retencija nije uspjela"
        else
            log "ERROR rclone upload failed (exit=$_rc)"
            exit 3
        fi
        ;;
    rsync)
        if ! command -v rsync >/dev/null 2>&1; then
            log "ERROR: rsync nije instaliran u image-u"
            exit 2
        fi
        log "rsync $LATEST → $DEST"
        if run_logged "$MAX_SEC" rsync -az --timeout=120 \
                -e "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15 -o ServerAliveCountMax=4" \
                "$LATEST" "$DEST"; then
            log "OK rsync upload"
            date '+%Y-%m-%d' > "$MARKER"
        else
            log "ERROR rsync upload failed (exit=$_rc)"
            exit 3
        fi
        ;;
    *)
        log "ERROR: nepoznat MODE=$MODE (rclone|rsync|disabled)"
        exit 4
        ;;
esac
