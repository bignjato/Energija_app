#!/bin/sh
# Sync orchestrator — sve outpute logiraj kroz `stamp` za timestamp.
# Logiranje: stdout (docker logs) + /data/sync.log s rotacijom (~10MB).

LOG=/data/sync.log
LOG_MAX_KB=10240

stamp() {
    while IFS= read -r line; do
        printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$line"
    done
}

rotate_log() {
    if [ -f "$LOG" ]; then
        size_kb=$(du -k "$LOG" 2>/dev/null | cut -f1)
        if [ -n "$size_kb" ] && [ "$size_kb" -gt "$LOG_MAX_KB" ]; then
            mv "$LOG" "${LOG}.1"
        fi
    fi
}

run() {
    rotate_log
    "$@" 2>&1 | stamp | tee -a "$LOG"
}

# Birač SMA izvora podataka (po prioritetu):
#   1. SMA_USE_HA_PULL=true → čita iz Home Assistant-a (brzo, lokalno)
#   2. SMA_USE_API=true     → službeni SMA Monitoring API
#   3. fallback              → stari Sunny Portal scrape
sma_recent() {
    if [ "${SMA_USE_HA_PULL:-false}" = "true" ]; then
        run python -m hepapp.ha_puller --only recent
        run python -m hepapp.ha_puller --only today
    elif [ "${SMA_USE_API:-false}" = "true" ]; then
        run python /app/sma_api_scraper.py --only recent
        run python /app/sma_api_scraper.py --only day
    else
        run python /app/sma_scraper.py
    fi
}

sma_history() {
    if [ "${SMA_USE_API:-false}" = "true" ]; then
        run python /app/sma_api_scraper.py --only month
    else
        run python /app/sma_history_import.py
    fi
}

sleep 30
echo "[boot] Prva sinkronizacija (SMA_USE_API=${SMA_USE_API:-false})..." | tee -a "$LOG"
run python /app/hep_scraper.py --dani 30
sma_recent
sma_history
run python /app/ha_sender.py

COUNTER=0
while true; do
    sleep 300
    COUNTER=$((COUNTER + 1))
    sma_recent

    # HEP + HA svakih sat (12 × 5min)
    if [ $((COUNTER % 12)) -eq 0 ]; then
        echo "[hourly] HEP + HA sync..." | tee -a "$LOG"
        run python /app/hep_scraper.py --dani 2
        run python /app/ha_sender.py
        # backfill dnevnih SMA flow-metrika + alert na stale podatke + anomalije
        run python /app/maintenance.py --backfill-daily --check-freshness --check-anomaly
    fi

    # SMA history jednom dnevno (288 × 5min = 24h)
    if [ $((COUNTER % 288)) -eq 0 ]; then
        echo "[daily] SMA history import..." | tee -a "$LOG"
        sma_history
        run python /app/maintenance.py --prune-live --keep-days 45 --bill-reminder
    fi

    # Backup baze jednom dnevno nakon 02:00. Marker datoteka umjesto COUNTER
    # faze — preživi restart kontejnera i ne ovisi o trenutku starta.
    HOUR=$(date +%H); HOUR=${HOUR#0}
    TODAY=$(date +%Y-%m-%d)
    LAST_BACKUP=$(cat /data/.last_backup_date 2>/dev/null)
    if [ "${HOUR:-0}" -ge 2 ] && [ "$LAST_BACKUP" != "$TODAY" ]; then
        BACKUP_DIR=/data/backups
        mkdir -p $BACKUP_DIR
        DATUM=$(date +%Y%m%d_%H%M)
        # sqlite3 .backup je konzistentan uz aktivan WAL (cp nije)
        if sqlite3 /data/hep_energy.db ".backup '$BACKUP_DIR/hep_energy_$DATUM.db'"; then
            echo "$TODAY" > /data/.last_backup_date
            ls -t $BACKUP_DIR/*.db | tail -n +8 | xargs rm -f 2>/dev/null
            echo "[backup] $BACKUP_DIR/hep_energy_$DATUM.db" | tee -a "$LOG"

            # Off-site upload (rclone ili rsync) — best effort, ne ruši loop
            /bin/sh /app/offsite_backup.sh || echo "[offsite] failed (ignored)" | tee -a "$LOG"
        else
            echo "[backup] sqlite3 .backup NEUSPJESAN" | tee -a "$LOG"
        fi
    fi
done
