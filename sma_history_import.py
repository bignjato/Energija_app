#!/usr/bin/env python3
"""
Import SMA povijesnih podataka - koristi Max aggregate
Formula: Max_W * 0.25h / 1000 = kWh po 15-min intervalu
"""
import requests, sqlite3, logging, os, sys
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

DB_PATH   = os.environ.get('DB_PATH', '/data/hep_energy.db')
TOKEN_URL = 'https://login.sma.energy/auth/realms/SMA/protocol/openid-connect/token'
API_BASE  = 'https://uiapi.sunnyportal.com/api/v1'
INV1_ID   = os.environ.get('SMA_INV1_ID', '')
INV2_ID   = os.environ.get('SMA_INV2_ID', '')

def get_token():
    r = requests.post(TOKEN_URL, data={
        'grant_type':'password','client_id':'SPpbeOS',
        'username':os.environ.get('SMA_USERNAME',''),'password':os.environ.get('SMA_PASSWORD',''),
        'scope':'openid profile'}, timeout=30)
    r.raise_for_status()
    return r.json()['access_token']

def fetch_week(token, begin_str, end_str, strict=False):
    # strict=True: HTTP greška baca iznimku (main), inače vraća prazno (maintenance.backfill_range)
    h = {'Authorization':f'Bearer {token}','Accept':'application/json','Content-Type':'application/json'}
    r = requests.post(f'{API_BASE}/measurements/search', headers=h, timeout=120, json={
        'dateTimeBegin': begin_str,
        'dateTimeEnd':   end_str,
        'queryItems': [
            {'componentId':INV1_ID,'channelId':'Measurement.GridMs.TotW.Pv','resolution':'FifteenMinutes','aggregate':'Max'},
            {'componentId':INV2_ID,'channelId':'Measurement.GridMs.TotW.Pv','resolution':'FifteenMinutes','aggregate':'Max'},
        ]
    })
    if r.status_code != 200:
        if strict:
            raise RuntimeError(f'SMA API {r.status_code}')
        log.warning(f'API {r.status_code}')
        return {}, {}
    inv1, inv2 = {}, {}
    for item in r.json():
        cid = item.get('componentId')
        for v in item.get('values',[]):
            ts  = v.get('time')
            val = float(v.get('value') or 0)
            if ts:
                if cid == INV1_ID: inv1[ts] = val
                else:              inv2[ts] = val
    return inv1, inv2

def aggregate_daily(conn):
    # W * 0.25h / 1000 = kWh po intervalu (Max aggregate precjenjuje stvarnu energiju)
    # ts je UTC ('...Z'), datum je LOKALNI dan -> date(ts,'localtime') (kontejner: TZ=Europe/Zagreb).
    # Upsert samo PV kolona: INSERT OR REPLACE bi obrisao cijeli red, pa i
    # feed_in/grid/total/autarky koje pišu drugi jobovi (ha_puller, maintenance).
    # Dan sa samim nulama dobije red, ali nula ne gazi već upisanu pozitivnu PV vrijednost.
    # Tekući lokalni dan: pv_generation_kwh se NE dira (samo inv1/inv2) — djelomični,
    # precijenjeni zbroj bi blokirao ha_puller.pull_today, koji prima samo rastuće vrijednosti.
    conn.execute('''
        INSERT INTO sma_dnevna
            (datum, pv_generation_kwh, pv_kwh_inv1, pv_kwh_inv2)
        SELECT date(ts, 'localtime'),
            CASE WHEN date(ts, 'localtime') <> date('now', 'localtime')
                 THEN ROUND(SUM(pv_w_total) * 0.25 / 1000.0, 3) END,
            ROUND(SUM(COALESCE(pv_w_inv1, 0)) * 0.25 / 1000.0, 3),
            ROUND(SUM(COALESCE(pv_w_inv2, 0)) * 0.25 / 1000.0, 3)
        FROM sma_15min
        WHERE pv_w_total IS NOT NULL AND date(ts, 'localtime') IS NOT NULL
        GROUP BY date(ts, 'localtime')
        ON CONFLICT(datum) DO UPDATE SET
            pv_generation_kwh = COALESCE(excluded.pv_generation_kwh, sma_dnevna.pv_generation_kwh),
            pv_kwh_inv1       = excluded.pv_kwh_inv1,
            pv_kwh_inv2       = excluded.pv_kwh_inv2
        WHERE excluded.pv_generation_kwh IS NULL
           OR excluded.pv_generation_kwh > 0
           OR COALESCE(sma_dnevna.pv_generation_kwh, 0) <= 0
    ''')
    conn.commit()

def main():
    conn = sqlite3.connect(DB_PATH)

    # Provjeri zadnji zapis
    row = conn.execute('SELECT MAX(ts), COUNT(*) FROM sma_15min').fetchone()
    log.info(f'Baza: {row[1]} zapisa, zadnji: {row[0]}')

    start = datetime(2022, 1, 1, tzinfo=timezone.utc)
    end   = datetime.now(timezone.utc)

    if row[0]:
        last = datetime.fromisoformat(row[0].replace('Z','+00:00'))
        start = last + timedelta(minutes=15)
        log.info(f'Nastavljam od {start.date()}')
    else:
        log.info(f'Puni import od {start.date()}')

    token    = get_token()
    token_ts = datetime.now()
    current  = start
    total    = 0
    failed   = False

    while current < end:
        week_end = min(current + timedelta(days=7), end)
        b = current.strftime('%Y-%m-%dT%H:%M:%S.000Z')
        e = week_end.strftime('%Y-%m-%dT%H:%M:%S.000Z')

        try:
            if (datetime.now() - token_ts).seconds > 3000:
                token = get_token()
                token_ts = datetime.now()

            inv1, inv2 = fetch_week(token, b, e, strict=True)
            all_ts = set(inv1)|set(inv2)
            saved = 0
            for ts in all_ts:
                w1, w2 = inv1.get(ts,0), inv2.get(ts,0)
                conn.execute('INSERT OR REPLACE INTO sma_15min (ts,pv_w_inv1,pv_w_inv2,pv_w_total) VALUES (?,?,?,?)',
                    (ts, w1, w2, w1+w2))
                saved += 1
            conn.commit()
            total += saved
            log.info(f'{current.date()} — {week_end.date()}: {saved} zapisa (ukupno {total})')
        except Exception as ex:
            # Ne idi dalje: kasniji tjedni bi pomaknuli MAX(ts) preko ovog i ostavili
            # trajnu rupu. Sljedeće pokretanje nastavlja od iste točke.
            conn.rollback()
            log.error(f'Greška {current.date()} — {week_end.date()}: {ex} — prekidam, sljedeći run ponavlja od {current.date()}')
            failed = True
            break

        current = week_end

    if failed:
        log.error(f'Import prekinut! Spremljeno {total} zapisa prije greške')
    else:
        log.info(f'Import gotov! Ukupno {total} zapisa')
    aggregate_daily(conn)

    count = conn.execute('SELECT COUNT(*) FROM sma_dnevna WHERE pv_generation_kwh>0').fetchone()[0]
    ukup  = conn.execute('SELECT SUM(pv_generation_kwh) FROM sma_dnevna').fetchone()[0] or 0
    log.info(f'Rezultat: {count} dana, {ukup:.1f} kWh ukupno')
    conn.close()
    return 1 if failed else 0

if __name__ == '__main__':
    sys.exit(main())
