"""Home Assistant REST API pull — alternativni izvor SMA podataka.

HA već lokalno (preko Modbus-a) razgovara sa SMA-om i ima sve live brojke
kao senzore. Mi te senzore čitamo i punimo sma_live + sma_dnevna.

Konfiguracija (env vars — entity_id-evi mogu biti zarezom razdvojene liste
koje se sumiraju, npr. dva invertera):

    HA_URL                       — npr. https://doma.example.com
    HA_TOKEN                     — long-lived access token
    HA_ENT_PV_W                  — solar power (W) — može biti lista
    HA_ENT_FEED_W                — feed-in power (W)
    HA_ENT_CONS_W                — total consumption (W) — opcionalno (computed ako fali)
    HA_ENT_GRID_W                — grid consumption (W)
    HA_ENT_AUTARKY               — autarky rate (0–100 ili 0–1) — opcionalno
    HA_ENT_BATTERY_SOC           — opcionalno
    HA_ENT_PV_KWH_DAY            — solar today (kWh ili Wh) — može biti lista
    HA_ENT_FEED_KWH_DAY          — feed-in today
    HA_ENT_CONS_KWH_DAY          — consumption today
    HA_ENT_GRID_KWH_DAY          — grid consumption today

    # Lifetime counter (kWh ili Wh) — ako fale dnevni senzori, izračunavamo
    # delta = current - vrijednost u 00:00 preko /api/history/period
    HA_ENT_PV_KWH_LIFETIME       — lifetime PV kWh (može biti lista)
    HA_ENT_FEED_KWH_LIFETIME     — lifetime feed-in kWh
    HA_ENT_GRID_KWH_LIFETIME     — lifetime grid consumption kWh
    HA_ENT_CONS_KWH_LIFETIME     — lifetime total consumption kWh

Auto-detektira unit (W vs kW, Wh vs kWh) i normalizira na W / kWh.
"""

import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone

from netutil import ha_verify, retry_session

DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')

VERIFY  = ha_verify()
SESSION = retry_session()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('ha_puller')


def _get_state(ha_url: str, token: str, entity_id: str) -> tuple:
    """Vrati (value: float, unit: str) ili (None, None)."""
    if not entity_id:
        return None, None
    url = f"{ha_url.rstrip('/')}/api/states/{entity_id}"
    try:
        r = SESSION.get(url, headers={'Authorization': f'Bearer {token}'}, timeout=10, verify=VERIFY)
        if r.status_code != 200:
            log.warning('HA %s: HTTP %s', entity_id, r.status_code)
            return None, None
        j = r.json()
        s = j.get('state')
        unit = (j.get('attributes', {}) or {}).get('unit_of_measurement', '')
        if s in (None, 'unknown', 'unavailable', ''):
            return None, unit
        try:
            return float(s), unit
        except ValueError:
            return None, unit
    except Exception as e:
        log.warning('HA %s: %s', entity_id, e)
        return None, None


def _normalize_power(value, unit: str) -> float:
    """Normaliziraj na W."""
    if value is None:
        return None
    u = (unit or '').lower()
    if u == 'kw':
        return value * 1000
    return value  # W


def _normalize_energy(value, unit: str) -> float:
    """Normaliziraj na kWh."""
    if value is None:
        return None
    u = (unit or '').lower()
    if u == 'wh':
        return value / 1000
    if u == 'mwh':
        return value * 1000
    return value  # kWh


def _midnight_state(ha_url: str, token: str, entity_id: str):
    """Vrati (value, unit) vrijednosti senzora u 00:00 danas (UTC).

    HA history endpoint: GET /api/history/period/{start}?filter_entity_id=X
    Vraća listu intervala — prvi state je vrijednost na start vremenu.
    """
    if not entity_id:
        return None, None
    # UTC ponoć danas — HA očekuje ISO 8601 s timezone offsetom
    today_midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    url = f"{ha_url.rstrip('/')}/api/history/period/{today_midnight}"
    try:
        r = SESSION.get(
            url,
            headers={'Authorization': f'Bearer {token}'},
            params={'filter_entity_id': entity_id, 'minimal_response': 'true'},
            timeout=15,
            verify=VERIFY,
        )
        if r.status_code != 200:
            log.warning('HA history %s: HTTP %s', entity_id, r.status_code)
            return None, None
        data = r.json()
        if not data or not data[0]:
            return None, None
        first = data[0][0]
        s = first.get('state')
        if s in (None, 'unknown', 'unavailable', ''):
            return None, None
        unit = (first.get('attributes', {}) or {}).get('unit_of_measurement', '')
        try:
            return float(s), unit
        except ValueError:
            return None, unit
    except Exception as e:
        log.warning('HA history %s: %s', entity_id, e)
        return None, None


def _today_delta(ha_url: str, token: str, entity_csv: str) -> float:
    """Izračunaj današnji prirast (kWh) iz lifetime kumulativnog brojača.

    Za svaki entity: current - midnight, normalizirano na kWh. Zbroji.
    """
    if not entity_csv:
        return None
    total = None
    for eid in entity_csv.split(','):
        eid = eid.strip()
        if not eid:
            continue
        now_v, now_u = _get_state(ha_url, token, eid)
        mid_v, mid_u = _midnight_state(ha_url, token, eid)
        if now_v is None or mid_v is None:
            continue
        delta = now_v - mid_v
        delta_kwh = _normalize_energy(delta, now_u or mid_u)
        total = (total or 0) + delta_kwh
    return total


def _sum_entities(ha_url: str, token: str, entity_csv: str, energy: bool = False) -> float:
    """Sumiraj listu zarezom razdvojenih entity_id-eva (s normalizacijom unita)."""
    if not entity_csv:
        return None
    total = None
    for eid in entity_csv.split(','):
        eid = eid.strip()
        if not eid:
            continue
        v, unit = _get_state(ha_url, token, eid)
        if v is None:
            continue
        v = _normalize_energy(v, unit) if energy else _normalize_power(v, unit)
        total = (total or 0) + v
    return total


def pull_recent():
    """Čitaj live W vrijednosti i spremi u sma_live."""
    ha_url = os.environ.get('HA_URL', '').strip()
    token  = os.environ.get('HA_TOKEN', '').strip()
    if not ha_url or not token:
        log.error('HA_URL ili HA_TOKEN nisu postavljeni')
        return False

    pv   = _sum_entities(ha_url, token, os.environ.get('HA_ENT_PV_W'))
    feed = _sum_entities(ha_url, token, os.environ.get('HA_ENT_FEED_W'))
    grid = _sum_entities(ha_url, token, os.environ.get('HA_ENT_GRID_W'))
    cons = _sum_entities(ha_url, token, os.environ.get('HA_ENT_CONS_W'))
    if cons is None and pv is not None and feed is not None and grid is not None:
        # Energy balance: pv = feed + cons_from_pv; cons = pv - feed + grid
        cons = max(0, pv - feed + grid)

    aut, _  = _get_state(ha_url, token, os.environ.get('HA_ENT_AUTARKY'))
    if aut is not None and aut > 1.5:
        aut = aut / 100.0
    soc, _ = _get_state(ha_url, token, os.environ.get('HA_ENT_BATTERY_SOC'))

    if pv is None and feed is None and cons is None:
        log.warning('HA: niti jedan W senzor ne vraća vrijednost. Provjeri HA_ENT_*.')
        return False

    conn = sqlite3.connect(DB_PATH)
    conn.execute('''
        INSERT INTO sma_live (ts, pv_generation_w, feed_in_w, external_consumption_w,
                              total_consumption_w, autarky_rate, battery_soc)
        VALUES (?,?,?,?,?,?,?)
    ''', (
        datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00'),
        pv, feed, grid, cons, aut, soc,
    ))
    conn.commit()
    conn.close()
    log.info('HA pull: PV=%sW feed=%sW cons=%sW grid=%sW aut=%s soc=%s',
             pv, feed, cons, grid, aut, soc)
    return True


def pull_today():
    """Čitaj današnje kWh totale i spremi u sma_dnevna za danas."""
    ha_url = os.environ.get('HA_URL', '').strip()
    token  = os.environ.get('HA_TOKEN', '').strip()
    if not ha_url or not token:
        log.error('HA_URL ili HA_TOKEN nisu postavljeni')
        return False

    pv_kwh   = _sum_entities(ha_url, token, os.environ.get('HA_ENT_PV_KWH_DAY'),   energy=True)
    feed_kwh = _sum_entities(ha_url, token, os.environ.get('HA_ENT_FEED_KWH_DAY'), energy=True)
    cons_kwh = _sum_entities(ha_url, token, os.environ.get('HA_ENT_CONS_KWH_DAY'), energy=True)
    grid_kwh = _sum_entities(ha_url, token, os.environ.get('HA_ENT_GRID_KWH_DAY'), energy=True)

    # Fallback: lifetime counter → izračunaj današnji delta iz history API-ja
    if pv_kwh is None:
        pv_kwh = _today_delta(ha_url, token, os.environ.get('HA_ENT_PV_KWH_LIFETIME'))
    if feed_kwh is None:
        feed_kwh = _today_delta(ha_url, token, os.environ.get('HA_ENT_FEED_KWH_LIFETIME'))
    if grid_kwh is None:
        grid_kwh = _today_delta(ha_url, token, os.environ.get('HA_ENT_GRID_KWH_LIFETIME'))
    if cons_kwh is None:
        cons_kwh = _today_delta(ha_url, token, os.environ.get('HA_ENT_CONS_KWH_LIFETIME'))

    if cons_kwh is None and pv_kwh is not None and feed_kwh is not None and grid_kwh is not None:
        cons_kwh = max(0, pv_kwh - feed_kwh + grid_kwh)

    if all(v is None for v in (pv_kwh, feed_kwh, cons_kwh, grid_kwh)):
        log.info('HA: nema dnevnih kWh senzora konfiguriranih.')
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
    ''', (today, pv_kwh, feed_kwh, grid_kwh, cons_kwh))
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
