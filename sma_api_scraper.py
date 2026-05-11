#!/usr/bin/env python3
"""SMA Monitoring API scraper.

Zamjena za sma_scraper.py (Sunny Portal scrape) — koristi službeni REST API
s OAuth2 autorizacijom. Daje **stvarne** podatke za današnji dan u 5-min
rezoluciji (feed_in, grid_consumption, total_consumption, pv_generation,
autarky_rate, self_consumption).

Piše u iste SQLite tablice kao stari scraper:
    sma_live        — najnoviji recent snapshot
    sma_15min       — 5-min set za današnji dan (overwrite)
    sma_dnevna      — dnevni sumarni iz Month endpointa

Aktivira se kada je u .env-u postavljen `SMA_USE_API=true` i SMA_API_*
credentialsi.

Pokreni:
    python sma_api_scraper.py [--dan YYYY-MM-DD] [--mjesec YYYY-MM]
"""

import argparse
import logging
import os
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from hepapp.sma_api import SmaApiClient

DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('sma_api_scraper')


def _ensure_schema(conn):
    """Idempotentno dodaj kolone ako fale (back-compat sa starim scraperom)."""
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS sma_live (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            pv_generation_w REAL, feed_in_w REAL, external_consumption_w REAL,
            total_consumption_w REAL, direct_consumption_w REAL,
            autarky_rate REAL, self_consumption_rate REAL, battery_soc REAL
        );
        CREATE TABLE IF NOT EXISTS sma_15min (
            ts TEXT PRIMARY KEY,
            pv_generation_wh REAL, feed_in_wh REAL, grid_consumption_wh REAL,
            total_consumption_wh REAL,
            pv_w_inv1 REAL DEFAULT 0, pv_w_inv2 REAL DEFAULT 0, pv_w_total REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS sma_dnevna (
            datum TEXT PRIMARY KEY,
            pv_generation_kwh REAL, feed_in_kwh REAL, grid_consumption_kwh REAL,
            total_consumption_kwh REAL, self_consumption_kwh REAL,
            autarky_rate REAL,
            pv_kwh_inv1 REAL, pv_kwh_inv2 REAL
        );
    ''')
    conn.commit()


def _save_recent(conn, plant_id: str, api: SmaApiClient):
    """Recent snapshot → sma_live."""
    j = api.energy_balance_recent(plant_id)
    series = j.get('set') or []
    if not series:
        log.warning('Recent: prazno')
        return 0
    last = series[-1]
    ts = last.get('time') or datetime.now().isoformat(timespec='seconds')
    conn.execute('''
        INSERT INTO sma_live (ts, pv_generation_w, feed_in_w, external_consumption_w,
                              total_consumption_w, direct_consumption_w,
                              autarky_rate, self_consumption_rate)
        VALUES (?,?,?,?,?,?,?,?)
    ''', (
        ts,
        last.get('pvGeneration'),
        last.get('gridFeedIn'),
        last.get('gridConsumption'),
        last.get('totalConsumption'),
        last.get('directConsumption'),
        last.get('autarkyRate'),
        last.get('selfConsumptionRate'),
    ))
    conn.commit()
    log.info('Recent: spremio sma_live @ %s (PV=%sW feed=%sW)', ts, last.get('pvGeneration'), last.get('gridFeedIn'))
    return 1


def _save_day(conn, plant_id: str, datum: str, api: SmaApiClient):
    """Day @ 5-min resolution → sma_15min, plus daily total → sma_dnevna."""
    j = api.energy_balance_day(plant_id, datum)
    series = j.get('set') or []
    total  = j.get('total') or {}
    log.info('Day %s: %d intervala (rezolucija %s)', datum, len(series), j.get('resolution'))

    # 5-min set
    n = 0
    for s in series:
        ts = s.get('time')
        if not ts:
            continue
        conn.execute('''
            INSERT INTO sma_15min (ts, pv_generation_wh, feed_in_wh, grid_consumption_wh,
                                   total_consumption_wh, pv_w_total)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(ts) DO UPDATE SET
                pv_generation_wh=excluded.pv_generation_wh,
                feed_in_wh=excluded.feed_in_wh,
                grid_consumption_wh=excluded.grid_consumption_wh,
                total_consumption_wh=excluded.total_consumption_wh,
                pv_w_total=excluded.pv_w_total
        ''', (
            ts,
            s.get('pvGeneration'),
            s.get('gridFeedIn'),
            s.get('gridConsumption'),
            s.get('totalConsumption'),
            s.get('pvGeneration'),  # W u 5-min ≈ Wh / (5/60) — za naše potrebe
        ))
        n += 1
    conn.commit()

    # Daily total
    if total:
        conn.execute('''
            INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, grid_consumption_kwh,
                                    total_consumption_kwh, self_consumption_kwh, autarky_rate)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(datum) DO UPDATE SET
                pv_generation_kwh=excluded.pv_generation_kwh,
                feed_in_kwh=excluded.feed_in_kwh,
                grid_consumption_kwh=excluded.grid_consumption_kwh,
                total_consumption_kwh=excluded.total_consumption_kwh,
                self_consumption_kwh=excluded.self_consumption_kwh,
                autarky_rate=excluded.autarky_rate
        ''', (
            datum,
            (total.get('pvGeneration')      or 0) / 1000,
            (total.get('gridFeedIn')        or 0) / 1000,
            (total.get('gridConsumption')   or 0) / 1000,
            (total.get('totalConsumption')  or 0) / 1000,
            (total.get('selfConsumption')   or 0) / 1000,
            total.get('autarkyRate'),
        ))
        conn.commit()
        log.info('Daily total %s: PV=%.2f kWh, feed=%.2f kWh, cons=%.2f kWh, autarkija=%.0f%%',
                 datum,
                 (total.get('pvGeneration')     or 0) / 1000,
                 (total.get('gridFeedIn')       or 0) / 1000,
                 (total.get('totalConsumption') or 0) / 1000,
                 (total.get('autarkyRate')      or 0) * 100)
    return n


def _save_month(conn, plant_id: str, mjesec: str, api: SmaApiClient):
    """Month → dnevni sumarni za cijeli mjesec u sma_dnevna."""
    j = api.energy_balance_month(plant_id, mjesec)
    series = j.get('set') or []
    log.info('Month %s: %d dana', mjesec, len(series))
    for s in series:
        ts = (s.get('time') or '')[:10]
        if not ts:
            continue
        conn.execute('''
            INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, grid_consumption_kwh,
                                    total_consumption_kwh, self_consumption_kwh, autarky_rate)
            VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(datum) DO UPDATE SET
                pv_generation_kwh=excluded.pv_generation_kwh,
                feed_in_kwh=excluded.feed_in_kwh,
                grid_consumption_kwh=excluded.grid_consumption_kwh,
                total_consumption_kwh=excluded.total_consumption_kwh,
                self_consumption_kwh=excluded.self_consumption_kwh,
                autarky_rate=excluded.autarky_rate
        ''', (
            ts,
            (s.get('pvGeneration')     or 0) / 1000,
            (s.get('gridFeedIn')       or 0) / 1000,
            (s.get('gridConsumption')  or 0) / 1000,
            (s.get('totalConsumption') or 0) / 1000,
            (s.get('selfConsumption')  or 0) / 1000,
            s.get('autarkyRate'),
        ))
    conn.commit()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dan',    default=datetime.now().strftime('%Y-%m-%d'),
                        help='Datum za Day endpoint (YYYY-MM-DD)')
    parser.add_argument('--mjesec', default=None,
                        help='Mjesec za Month endpoint (YYYY-MM); default = trenutni')
    parser.add_argument('--only',   choices=['recent', 'day', 'month', 'all'], default='all')
    args = parser.parse_args()

    plant_id = os.environ.get('SMA_API_PLANT_ID', '').strip()
    if not plant_id:
        log.error('SMA_API_PLANT_ID nije postavljen u .env')
        sys.exit(2)

    api = SmaApiClient()

    conn = sqlite3.connect(DB_PATH)
    _ensure_schema(conn)

    try:
        if args.only in ('recent', 'all'):
            _save_recent(conn, plant_id, api)
        if args.only in ('day', 'all'):
            _save_day(conn, plant_id, args.dan, api)
        if args.only in ('month', 'all'):
            mj = args.mjesec or datetime.now().strftime('%Y-%m')
            _save_month(conn, plant_id, mj, api)
    finally:
        conn.close()


if __name__ == '__main__':
    main()
