#!/bin/sh
# Sync orchestrator — sve outpute logiraj kroz `ts` za timestamp.
# Logiranje: stdout (docker logs) + opcionalno /data/sync.log s rotacijom.

LOG=/data/sync.log
LOG_MAX_KB=10240   # ~10 MB

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

sleep 30
echo "[boot] Prva sinkronizacija..." | tee -a "$LOG"
run python /app/hep_scraper.py --dani 30
run python /app/sma_scraper.py
run python /app/sma_history_import.py
run python /app/ha_sender.py

COUNTER=0
while true; do
    sleep 300
    COUNTER=$((COUNTER + 1))
    run python /app/sma_scraper.py

    # HEP + HA svakih sat (12 × 5min)
    if [ $((COUNTER % 12)) -eq 0 ]; then
        echo "[hourly] HEP + HA sync..." | tee -a "$LOG"
        run python /app/hep_scraper.py --dani 2
        run python /app/ha_sender.py
    fi

    # SMA history jednom dnevno (288 × 5min = 24h)
    if [ $((COUNTER % 288)) -eq 0 ]; then
        echo "[daily] SMA history import..." | tee -a "$LOG"
        run python /app/sma_history_import.py
    fi

    # Backup baze u 02:00
    HOUR=$(date +%H)
    if [ $((COUNTER % 288)) -eq 144 ] && [ "$HOUR" = "02" ]; then
        BACKUP_DIR=/data/backups
        mkdir -p $BACKUP_DIR
        DATUM=$(date +%Y%m%d_%H%M)
        cp /data/hep_energy.db $BACKUP_DIR/hep_energy_$DATUM.db
        ls -t $BACKUP_DIR/*.db | tail -n +8 | xargs rm -f 2>/dev/null
        echo "[backup] $BACKUP_DIR/hep_energy_$DATUM.db" | tee -a "$LOG"
    fi
done
