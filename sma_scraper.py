#!/usr/bin/env python3
"""
SMA Sunny Portal scraper - dohvaca live i povijesne podatke
Koristi uiapi.sunnyportal.com s JWT autentikacijom
"""

import requests
import sqlite3
import logging
import os
import json
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

# --- Konfiguracija ---
SMA_USERNAME   = os.environ.get('SMA_USERNAME', 'boris@infobot.hr')
SMA_PASSWORD   = os.environ.get('SMA_PASSWORD', 'qibhib-qywbuJ-1jujji')
SMA_CLIENT_ID  = 'SPpbeOS'
SMA_TOKEN_URL  = 'https://login.sma.energy/auth/realms/SMA/protocol/openid-connect/token'
SMA_API_BASE   = 'https://uiapi.sunnyportal.com/api/v1'

# Component IDs
SMA_PLANT_ID   = '12821140'   # FNE Ignjatović Boris (root, 30kW)
SMA_HM_ID      = '12821779'   # Home Manager 2.0
SMA_KUCA_ID    = '12821195'   # Kuća
SMA_INV1_ID    = '12821155'   # Sunny Tripower 10.0
SMA_INV2_ID    = '12821156'   # SMA Tripower 20.0

DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')


def get_sma_token():
    """Dohvati JWT access token od SMA"""
    r = requests.post(SMA_TOKEN_URL, data={
        'grant_type': 'password',
        'client_id': SMA_CLIENT_ID,
        'username': SMA_USERNAME,
        'password': SMA_PASSWORD,
        'scope': 'openid profile',
    }, timeout=60)
    r.raise_for_status()
    return r.json()['access_token']


def sma_get(token, endpoint, params=None):
    """GET request na SMA API"""
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
    }
    r = requests.get(f'{SMA_API_BASE}{endpoint}', headers=headers, params=params, timeout=60)
    if r.status_code == 200 and '<html' not in r.text[:30]:
        return r.json()
    return None


def sma_post(token, endpoint, payload):
    """POST request na SMA API"""
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
    }
    r = requests.post(f'{SMA_API_BASE}{endpoint}', headers=headers, json=payload, timeout=60)
    if r.status_code == 200:
        return r.json()
    log.warning(f'SMA POST {endpoint}: {r.status_code} {r.text[:200]}')
    return None


def init_sma_tables(conn):
    """Kreiraj SMA tablice u bazi"""
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sma_live (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            pv_generation_w REAL,
            feed_in_w REAL,
            external_consumption_w REAL,
            total_consumption_w REAL,
            direct_consumption_w REAL,
            autarky_rate REAL,
            self_consumption_rate REAL,
            battery_soc REAL
        );

        CREATE TABLE IF NOT EXISTS sma_15min (
            ts TEXT NOT NULL,
            pv_generation_wh REAL,
            feed_in_wh REAL,
            grid_consumption_wh REAL,
            total_consumption_wh REAL,
            PRIMARY KEY (ts)
        );

        CREATE TABLE IF NOT EXISTS sma_dnevna (
            datum TEXT NOT NULL PRIMARY KEY,
            pv_generation_kwh REAL,
            feed_in_kwh REAL,
            grid_consumption_kwh REAL,
            total_consumption_kwh REAL,
            self_consumption_kwh REAL,
            autarky_rate REAL
        );

        CREATE TABLE IF NOT EXISTS tarife (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            naziv TEXT NOT NULL,
            cijena_kupnja REAL NOT NULL,
            cijena_prodaja REAL,
            vt_pocetak INTEGER DEFAULT 7,
            vt_kraj INTEGER DEFAULT 21,
            aktivan INTEGER DEFAULT 1,
            stvoren TEXT DEFAULT (datetime('now'))
        );

        -- Defaultna tarifa ako ne postoji
        INSERT OR IGNORE INTO tarife (id, naziv, cijena_kupnja, cijena_prodaja)
        VALUES (1, 'HEP standardna', 0.12, 0.065);
    ''')
    conn.commit()
    log.info('SMA tablice inicijalizirane')


def fetch_live(token, conn):
    """Dohvati i pohrani live podatke"""
    data = sma_get(token, f'/widgets/energybalance?componentId={SMA_PLANT_ID}')
    if not data:
        log.error('Nije moguće dohvatiti live podatke')
        return None

    ts = data.get('time', datetime.now().isoformat())
    row = (
        ts,
        data.get('pvGeneration'),
        data.get('feedIn'),
        data.get('externalConsumption'),
        data.get('totalConsumption'),
        data.get('directConsumption'),
        data.get('autarkyRate'),
        data.get('selfConsumptionRate'),
        data.get('batteryStateOfCharge'),
    )
    conn.execute('''
        INSERT INTO sma_live (ts, pv_generation_w, feed_in_w, external_consumption_w,
            total_consumption_w, direct_consumption_w, autarky_rate, self_consumption_rate, battery_soc)
        VALUES (?,?,?,?,?,?,?,?,?)
    ''', row)
    conn.commit()
    log.info(f'Live: PV={data.get("pvGeneration")}W, feedIn={data.get("feedIn")}W, '
             f'potrosnja={data.get("totalConsumption")}W, autarkija={data.get("autarkyRate",0)*100:.0f}%')
    return data


def fetch_historical_15min(token, conn, datum_od=None, datum_do=None):
    """Dohvati 15-min podatke za period"""
    if datum_od is None:
        # Zadnji pohranjeni datum
        row = conn.execute('SELECT MAX(ts) FROM sma_15min').fetchone()
        if row[0]:
            datum_od = datetime.fromisoformat(row[0]) + timedelta(minutes=15)
        else:
            datum_od = datetime.now() - timedelta(days=30)

    if datum_do is None:
        datum_do = datetime.now()

    # Dohvati po danima
    current = datum_od.replace(hour=0, minute=0, second=0, microsecond=0)
    total_saved = 0

    while current <= datum_do:
        day_end = current + timedelta(days=1)
        begin_str = current.strftime('%Y-%m-%dT00:00:00Z')
        end_str = day_end.strftime('%Y-%m-%dT00:00:00Z')

        # Dohvati sve kanale odjednom
        payload = {
            'queryItems': [
                {'componentId': SMA_PLANT_ID, 'channelId': 'GridMs.TotW', 'aggregate': 'Sum'},
            ],
            'dateTimeBegin': begin_str,
            'dateTimeEnd': end_str,
            'resolution': 'QuarterHour',
        }
        results = sma_post(token, '/measurements/search', payload)

        if results:
            saved = 0
            for item in results:
                cid = item.get('componentId')
                ch  = item.get('channelId')
                for v in item.get('values', []):
                    ts_val = v.get('timestamp') or v.get('time')
                    val    = v.get('value')
                    if ts_val and val is not None:
                        # Pohrani ovisno o kanalu/komponentu
                        try:
                            conn.execute('''
                                INSERT OR IGNORE INTO sma_15min (ts, pv_generation_wh)
                                VALUES (?, ?)
                            ''', (ts_val, val))
                            saved += 1
                        except Exception as e:
                            log.debug(f'Skip: {e}')
            if saved:
                conn.commit()
                total_saved += saved

        current += timedelta(days=1)

    log.info(f'Pohranjeno {total_saved} SMA 15-min zapisa')
    return total_saved


def aggregate_sma_daily(conn):
    """Agregiraj dnevne SMA podatke iz live tablice"""
    # Agregiraj iz live zapisa (svakih 5-15 min)
    conn.execute('''
        INSERT OR REPLACE INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, 
            grid_consumption_kwh, total_consumption_kwh, self_consumption_kwh, autarky_rate)
        SELECT 
            date(ts) as datum,
            -- W * interval_sati = Wh, dijeli s 1000 za kWh
            -- Live zapisi su svakih ~5 min = 1/12 sata
            ROUND(SUM(COALESCE(pv_generation_w, 0)) / 12.0 / 1000.0, 3) as pv_kwh,
            ROUND(SUM(COALESCE(feed_in_w, 0)) / 12.0 / 1000.0, 3) as feed_in_kwh,
            ROUND(SUM(COALESCE(external_consumption_w, 0)) / 12.0 / 1000.0, 3) as grid_kwh,
            ROUND(SUM(COALESCE(total_consumption_w, 0)) / 12.0 / 1000.0, 3) as total_kwh,
            ROUND(SUM(COALESCE(direct_consumption_w, 0)) / 12.0 / 1000.0, 3) as self_kwh,
            AVG(COALESCE(autarky_rate, 0)) as autarky
        FROM sma_live
        WHERE date(ts) < date('now')
        GROUP BY date(ts)
    ''')
    conn.commit()
    log.info('SMA dnevna agregacija završena')


def main():
    conn = sqlite3.connect(DB_PATH)
    init_sma_tables(conn)

    try:
        log.info('Dohvaćam SMA token...')
        token = get_sma_token()

        log.info('Dohvaćam live podatke...')
        fetch_live(token, conn)

        log.info('Agregiram dnevne podatke...')
        aggregate_sma_daily(conn)

    except Exception as e:
        log.error(f'Greška: {e}', exc_info=True)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
