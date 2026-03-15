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

    if [ $((COUNTER % 12)) -eq 0 ]; then
        echo "[$(date)] HEP + HA sync..."
        python /app/hep_scraper.py --dani 2
        python /app/ha_sender.py
    fi

    # Jednom dnevno (288 × 5min = 24h) SMA history import
    if [ $((COUNTER % 288)) -eq 0 ]; then
        echo "[$(date)] SMA history import..."
        python /app/sma_history_import.py
    fi
done
