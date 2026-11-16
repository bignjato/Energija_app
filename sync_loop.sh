#!/bin/sh
# Sync orchestrator — sve outpute logiraj kroz `stamp` za timestamp.
# Logiranje: stdout (docker logs) + /data/sync.log s rotacijom (~10MB).
#
# Svaki korak ide kroz coreutils `timeout` (jedan obješeni mrežni poziv ne smije
# zaustaviti cijeli loop, a s njim i alerte koji bi taj zastoj prijavili).
# Satni/dnevni blok raspoređuju se po zidnom satu uz marker datoteke u /data
# (preživljavaju restart kontejnera), ne po brojaču iteracija.
#
# Env override-i (defaulti = produkcija; koriste ih i testovi):
#   SYNC_DATA_DIR (/data), SYNC_INTERVAL (300 s), SYNC_BOOT_DELAY (30 s),
#   SYNC_DAILY_HOUR (6), SYNC_BACKUP_HOUR (2), SYNC_MAX_ITER (0 = beskonačno)

DATA_DIR="${SYNC_DATA_DIR:-/data}"
LOG="$DATA_DIR/sync.log"
LOG_MAX_KB=10240
DB="${DB_PATH:-$DATA_DIR/hep_energy.db}"
HEARTBEAT="$DATA_DIR/.sync_heartbeat"
INTERVAL="${SYNC_INTERVAL:-300}"
BOOT_DELAY="${SYNC_BOOT_DELAY:-30}"
DAILY_HOUR="${SYNC_DAILY_HOUR:-6}"
BACKUP_HOUR="${SYNC_BACKUP_HOUR:-2}"
MAX_ITER="${SYNC_MAX_ITER:-0}"

# Limiti po koraku (sekunde). Svi su namjerno < 20 min: heartbeat se dira nakon
# svakog koraka, a healthcheck (docker-compose) traži heartbeat mlađi od 20 min.
T_STEP=240          # koraci 5-min ciklusa (SMA recent, HA push)
T_HEP=900           # HEP portal scrape
T_HISTORY=900       # dnevni SMA history import
T_MAINT=600         # maintenance.py
T_BACKUP=300        # sqlite3 .backup
T_OFFSITE=900       # off-site upload; mora biti > zbroja limita u offsite_backup.sh (840)
T_KILL=30           # SIGKILL ako proces ignorira SIGTERM

stamp() {
    while IFS= read -r line; do
        printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$line"
    done
}

log() {
    printf '%s\n' "$*" | stamp | tee -a "$LOG"
}

rotate_log() {
    if [ -f "$LOG" ]; then
        size_kb=$(du -k "$LOG" 2>/dev/null | cut -f1)
        if [ -n "$size_kb" ] && [ "$size_kb" -gt "$LOG_MAX_KB" ]; then
            mv "$LOG" "${LOG}.1"
        fi
    fi
}

heartbeat() {
    touch "$HEARTBEAT" 2>/dev/null || true
}

if command -v timeout >/dev/null 2>&1; then
    HAVE_TIMEOUT=1
else
    HAVE_TIMEOUT=0
fi

# with_timeout SEKUNDE naredba... — exit 124 = istekao limit, 137 = SIGKILL
with_timeout() {
    _limit=$1; shift
    if [ "$HAVE_TIMEOUT" = 1 ]; then
        timeout -k "$T_KILL" "$_limit" "$@"
    else
        "$@"
    fi
}

# Limit se bira prema naredbi, da pozivi `run python ...` ostanu nepromijenjeni.
step_limit() {
    case "$*" in
        *hep_scraper.py*)                           echo "$T_HEP" ;;
        *sma_history_import.py*|*"--only month"*)   echo "$T_HISTORY" ;;
        *maintenance.py*)                           echo "$T_MAINT" ;;
        *)                                          echo "$T_STEP" ;;
    esac
}

# Pokreni korak s timeoutom; output ide kroz stamp u log. Običan sh nema
# pipefail, pa `cmd | stamp | tee` vraća status tee-a — zato se status naredbe
# šalje kroz fd 3 (hvata ga $(...)), a output koraka kroz fd 4 na pravi stdout.
run() {
    rotate_log
    _limit=$(step_limit "$@")
    exec 4>&1
    _rc=$(
        {
            { with_timeout "$_limit" "$@" 2>&1 3>&- 4>&-; echo $? >&3; } \
                | stamp 3>&- | tee -a "$LOG" >&4 3>&-
        } 3>&1
    )
    exec 4>&-
    case "$_rc" in
        0)       ;;
        124|137) log "[run] TIMEOUT nakon ${_limit}s (exit=$_rc): $*" ;;
        '')      _rc=1; log "[run] GRESKA (nepoznat exit status): $*" ;;
        *)       log "[run] GRESKA exit=$_rc: $*" ;;
    esac
    heartbeat
    return "$_rc"
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

heartbeat
[ "$HAVE_TIMEOUT" = 1 ] || log "[boot] UPOZORENJE: 'timeout' nije dostupan — koraci idu bez vremenskog limita"
sleep "$BOOT_DELAY"
log "[boot] Prva sinkronizacija (SMA_USE_API=${SMA_USE_API:-false})..."
run python /app/hep_scraper.py --dani 30
sma_recent
sma_history
run python /app/ha_sender.py

ITER=0
while true; do
    sleep "$INTERVAL"
    sma_recent
    run python /app/ha_push_intel.py

    # HEP + HA jednom po satu zidnog sata. Marker u UTC-u (bez DST dvosmislenosti)
    # preživi restart, pa deploy ne pomiče fazu niti iteracije "driftaju".
    HOUR_NOW=$(date -u +%Y-%m-%dT%H)
    LAST_HOURLY=$(cat "$DATA_DIR/.last_hourly" 2>/dev/null)
    if [ "$LAST_HOURLY" != "$HOUR_NOW" ]; then
        log "[hourly] HEP + HA sync..."
        run python /app/hep_scraper.py --dani 2
        run python /app/ha_sender.py
        # backfill dnevnih SMA flow-metrika + alert na stale podatke + anomalije
        run python /app/maintenance.py --backfill-daily --check-freshness --check-anomaly
        echo "$HOUR_NOW" > "$DATA_DIR/.last_hourly"
    fi

    HOUR=$(date +%H); HOUR=${HOUR#0}
    TODAY=$(date +%Y-%m-%d)

    # SMA history + dnevno održavanje jednom po lokalnom danu, nakon DAILY_HOUR
    # (ujutro: nakon backupa u 02:00, pa restore drill i offsite provjera vide
    # svježe stanje, a obavijesti ne stižu usred noći).
    # Marker se piše i kad neki korak padne (greška je u logu) — bez toga bi se
    # cijeli blok ponavljao svakih 5 min do ponoći.
    LAST_DAILY=$(cat "$DATA_DIR/.last_daily_date" 2>/dev/null)
    if [ "${HOUR:-0}" -ge "$DAILY_HOUR" ] && [ "$LAST_DAILY" != "$TODAY" ]; then
        log "[daily] SMA history import..."
        sma_history
        run python /app/maintenance.py --prune-live --keep-days 45 --bill-reminder \
            --restore-drill --check-offsite --check-token-age --check-inverter
        echo "$TODAY" > "$DATA_DIR/.last_daily_date"
    fi

    # Backup baze jednom dnevno nakon 02:00. Marker datoteka umjesto COUNTER
    # faze — preživi restart kontejnera i ne ovisi o trenutku starta.
    LAST_BACKUP=$(cat "$DATA_DIR/.last_backup_date" 2>/dev/null)
    if [ "${HOUR:-0}" -ge "$BACKUP_HOUR" ] && [ "$LAST_BACKUP" != "$TODAY" ]; then
        BACKUP_DIR="$DATA_DIR/backups"
        mkdir -p "$BACKUP_DIR"
        DATUM=$(date +%Y%m%d_%H%M)
        BACKUP_FILE="$BACKUP_DIR/hep_energy_$DATUM.db"
        # sqlite3 .backup je konzistentan uz aktivan WAL (cp nije)
        with_timeout "$T_BACKUP" sqlite3 "$DB" ".backup '$BACKUP_FILE'" 2>&1 | stamp | tee -a "$LOG"
        # status preko integrity provjere kopije: pipeline iznad vraća status tee-a
        if [ -s "$BACKUP_FILE" ] && \
           [ "$(with_timeout "$T_BACKUP" sqlite3 "$BACKUP_FILE" 'PRAGMA quick_check;' 2>/dev/null)" = "ok" ]; then
            echo "$TODAY" > "$DATA_DIR/.last_backup_date"
            ls -t "$BACKUP_DIR"/*.db | tail -n +8 | xargs rm -f 2>/dev/null
            log "[backup] $BACKUP_FILE"
            heartbeat

            # Off-site upload (rclone ili rsync) — best effort, ne ruši loop
            with_timeout "$T_OFFSITE" /bin/sh /app/offsite_backup.sh
            RC=$?
            [ "$RC" -eq 0 ] || log "[offsite] GRESKA exit=$RC (ignored, loop ide dalje)"
        else
            # nepotpunu kopiju ne ostavljaj — restore drill / offsite bi je uzeli kao zadnju
            rm -f "$BACKUP_FILE"
            log "[backup] GRESKA: sqlite3 .backup NEUSPJESAN ($DB)"
        fi
    fi

    heartbeat
    ITER=$((ITER + 1))
    if [ "$MAX_ITER" -gt 0 ] && [ "$ITER" -ge "$MAX_ITER" ]; then
        log "[loop] SYNC_MAX_ITER=$MAX_ITER dosegnut — izlaz"
        exit 0
    fi
done
