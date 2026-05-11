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

BACKUP_DIR=/data/backups
LOG=/data/sync.log
MODE="${OFFSITE_BACKUP_MODE:-disabled}"
DEST="${OFFSITE_BACKUP_DEST:-}"
KEEP="${OFFSITE_BACKUP_KEEP:-14}"

log() {
    printf '[%s] [offsite] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" | tee -a "$LOG"
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
        if rclone copy "$LATEST" "$DEST" 2>&1 | tee -a "$LOG"; then
            log "OK rclone upload"
            # Retencija na remote
            rclone delete --min-age "${KEEP}d" "$DEST" 2>&1 | tee -a "$LOG" || true
        else
            log "ERROR rclone upload failed"
            exit 3
        fi
        ;;
    rsync)
        if ! command -v rsync >/dev/null 2>&1; then
            log "ERROR: rsync nije instaliran u image-u"
            exit 2
        fi
        log "rsync $LATEST → $DEST"
        if rsync -az -e "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes" \
                 "$LATEST" "$DEST" 2>&1 | tee -a "$LOG"; then
            log "OK rsync upload"
        else
            log "ERROR rsync upload failed"
            exit 3
        fi
        ;;
    *)
        log "ERROR: nepoznat MODE=$MODE (rclone|rsync|disabled)"
        exit 4
        ;;
esac
