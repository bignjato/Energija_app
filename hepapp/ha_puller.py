"""Home Assistant REST API pull — alternativa SMA Monitoring API-ju.

Home Assistant već lokalno (preko Modbus-a) razgovara sa SMA Sunny Home
Managerom / inverterom i ima senzore s točnim live brojkama. Mi te senzore
možemo *čitati* (REST API GET /api/states/<entity_id>) i puniti našu
sma_live tablicu — bez SMA Monitoring API registracije, bez čekanja.

Konfiguracija (env vars):
    HA_URL                       — npr. https://doma.example.com:8123
    HA_TOKEN                     — long-lived access token
    HA_ENT_PV_W                  — entity_id za solar power (W), npr. sensor.sma_pv_power
    HA_ENT_FEED_W                — entity_id za feed-in power (W)
    HA_ENT_CONS_W                — entity_id za total consumption power (W)
    HA_ENT_GRID_W                — entity_id za grid consumption power (W)
    HA_ENT_AUTARKY               — entity_id za autarky rate (0–100 ili 0–1)
    HA_ENT_BATTERY_SOC           — (opcionalno) battery state of charge
    HA_ENT_PV_KWH_DAY            — entity_id za solar energy today (kWh) — opcionalno
    HA_ENT_FEED_KWH_DAY          — feed_in energy today (kWh)
    HA_ENT_CONS_KWH_DAY          — total consumption energy today (kWh)
    HA_ENT_GRID_KWH_DAY          — grid consumption energy today (kWh)

Endpoint: GET <HA_URL>/api/states/<entity_id> → {"state":"<number>", ...}
"""

import logging
import os
import sqlite3
import sys
from datetime import datetime

import requests

DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('ha_puller')


def _get_state(ha_url: str, token: str, entity_id: str):
    if not entity_id:
        return None
    url = f"{ha_url.rstrip('/')}/api/states/{entity_id}"
    try:
        r = requests.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=10, verify=False)
        if r.status_code != 200:
            log.warning('HA %s: HTTP %s', entity_id, r.status_code)
            return None
        s = r.json().get('state')
        if s in (None, 'unknown', 'unavailable', ''):
            return None
        try:
            return float(s)
        except ValueError:
            return None
    except Exception as e:
        log.warning('HA %s: %s', entity_id, e)
        return None


def pull_recent():
    """Čitaj live W vrijednosti iz HA i spremi u sma_live."""
    ha_url = os.environ.get('HA_URL', '').strip()
    token  = os.environ.get('HA_TOKEN', '').strip()
    if not ha_url or not token:
        log.error('HA_URL ili HA_TOKEN nisu postavljeni u .env')
        return False

    pv   = _get_state(ha_url, token, os.environ.get('HA_ENT_PV_W'))
    feed = _get_state(ha_url, token, os.environ.get('HA_ENT_FEED_W'))
    cons = _get_state(ha_url, token, os.environ.get('HA_ENT_CONS_W'))
    grid = _get_state(ha_url, token, os.environ.get('HA_ENT_GRID_W'))
    aut  = _get_state(ha_url, token, os.environ.get('HA_ENT_AUTARKY'))
    soc  = _get_state(ha_url, token, os.environ.get('HA_ENT_BATTERY_SOC'))

    # Autarkija normalizacija: ako je 0–100, podijeli s 100
    if aut is not None and aut > 1:
        aut = aut / 100.0

    if pv is None and feed is None and cons is None:
        log.warning('HA: niti jedan W senzor ne vraća vrijednost. Provjeri HA_ENT_*.')
        return False

    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        INSERT INTO sma_live (ts, pv_generation_w, feed_in_w, external_consumption_w,
                              total_consumption_w, autarky_rate, battery_soc)
        VALUES (?,?,?,?,?,?,?)
    ''', (
        datetime.now().isoformat(timespec='seconds'),
        pv, feed, grid, cons, aut, soc,
    ))
    conn.commit()
    conn.close()
    log.info('HA pull: PV=%sW feed=%sW cons=%sW grid=%sW aut=%s soc=%s',
             pv, feed, cons, grid, aut, soc)
    return True


def pull_today():
    """Čitaj današnje kWh totale iz HA i spremi u sma_dnevna za danas."""
    ha_url = os.environ.get('HA_URL', '').strip()
    token  = os.environ.get('HA_TOKEN', '').strip()
    if not ha_url or not token:
        log.error('HA_URL ili HA_TOKEN nisu postavljeni')
        return False

    pv_kwh   = _get_state(ha_url, token, os.environ.get('HA_ENT_PV_KWH_DAY'))
    feed_kwh = _get_state(ha_url, token, os.environ.get('HA_ENT_FEED_KWH_DAY'))
    cons_kwh = _get_state(ha_url, token, os.environ.get('HA_ENT_CONS_KWH_DAY'))
    grid_kwh = _get_state(ha_url, token, os.environ.get('HA_ENT_GRID_KWH_DAY'))

    if all(v is None for v in (pv_kwh, feed_kwh, cons_kwh, grid_kwh)):
        log.info('HA: nema dnevnih kWh senzora konfiguriranih (HA_ENT_*_KWH_DAY).')
        return False

    today = datetime.now().strftime('%Y-%m-%d')
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, grid_consumption_kwh,
                                total_consumption_kwh)
        VALUES (?,?,?,?,?)
        ON CONFLICT(datum) DO UPDATE SET
            pv_generation_kwh    = COALESCE(excluded.pv_generation_kwh, sma_dnevna.pv_generation_kwh),
            feed_in_kwh          = COALESCE(excluded.feed_in_kwh, sma_dnevna.feed_in_kwh),
            grid_consumption_kwh = COALESCE(excluded.grid_consumption_kwh, sma_dnevna.grid_consumption_kwh),
            total_consumption_kwh= COALESCE(excluded.total_consumption_kwh, sma_dnevna.total_consumption_kwh)
    ''', (today, pv_kwh, feed_kwh, cons_kwh, grid_kwh))
    conn.commit()
    conn.close()
    log.info('HA pull today: PV=%s kWh, feed=%s kWh, cons=%s kWh, grid=%s kWh',
             pv_kwh, feed_kwh, cons_kwh, grid_kwh)
    return True


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--only', choices=['recent', 'today', 'all'], default='all')
    args = parser.parse_args()
    ok = True
    if args.only in ('recent', 'all'):
        ok = pull_recent() and ok
    if args.only in ('today', 'all'):
        ok = pull_today() and ok
    sys.exit(0 if ok else 1)
