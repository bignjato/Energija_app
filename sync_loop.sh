#!/bin/sh
sleep 30
echo "Prva sinkronizacija..."
python /app/hep_scraper.py --dani 30
python /app/sma_scraper.py
python /app/sma_history_import.py
python /app/ha_sender.py

COUNTER=0
while true; do
    sleep 300
    COUNTER=$((COUNTER + 1))
    python /app/sma_scraper.py

    # HEP + HA svakih sat (12 × 5min)
    if [ $((COUNTER % 12)) -eq 0 ]; then
        echo "[$(date)] HEP + HA sync..."
        python /app/hep_scraper.py --dani 2
        python /app/ha_sender.py
    fi

    # SMA history jednom dnevno (288 × 5min = 24h)
    if [ $((COUNTER % 288)) -eq 0 ]; then
        echo "[$(date)] SMA history import..."
        python /app/sma_history_import.py
    fi

    # Backup baze jednom dnevno u 02:00
    HOUR=$(date +%H)
    if [ $((COUNTER % 288)) -eq 144 ] && [ "$HOUR" = "02" ]; then
        echo "[$(date)] Backup baze..."
        BACKUP_DIR=/data/backups
        mkdir -p $BACKUP_DIR
        DATUM=$(date +%Y%m%d_%H%M)
        cp /data/hep_energy.db $BACKUP_DIR/hep_energy_$DATUM.db
        # Zadrži zadnjih 7 backupa
        ls -t $BACKUP_DIR/*.db | tail -n +8 | xargs rm -f 2>/dev/null
        echo "[$(date)] Backup: $BACKUP_DIR/hep_energy_$DATUM.db"
    fi
done
