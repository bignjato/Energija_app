#!/usr/bin/env python3
"""Gura energetsku/financijsku inteligenciju iz aplikacije u Home Assistant.

Senzori (app -> HA):
  sensor.energija_solarni_visak          W    trenutni visak (izvoz u mrezu)
  binary_sensor.energija_isplati_se_trositi   on/off  visak > prag
  sensor.energija_dug                    EUR  zadnji dug s racuna
  sensor.energija_trosak_mjesec          EUR  trosak struje ovaj mjesec do sada
  sensor.energija_trosak_danas           EUR  trosak struje danas (SMA)
  sensor.energija_vt_udio                %    sluzbeni VT udio (registri)
  sensor.energija_autarkija_danas        %    autarkija danas (SMA)

Kad su podaci iza senzora zastarjeli/nepoznati, gura se stanje 'unavailable'
(s istim atributima) — inace bi u HA zauvijek ostala zadnja (kriva) vrijednost.

Pokretanje: python /app/ha_push_intel.py
Prag viska: env VISAK_PRAG_W (default 500).
"""
import os, sqlite3, logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('ha_push_intel')

DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')
VISAK_PRAG_W = float(os.environ.get('VISAK_PRAG_W', '500'))
LIVE_MAX_AGE_MIN = 30          # sma_live stariji od ovoga = zastario
LOCAL_TZ = ZoneInfo('Europe/Zagreb')
UNAVAILABLE = 'unavailable'


def _now_utc():
    """Trenutno vrijeme (aware, UTC). Izdvojeno radi testova."""
    return datetime.now(timezone.utc)


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _avg_buy_price(vt_udio):
    from hepapp.tariff import get_tarifa
    t = get_tarifa()   # trenutno vazeci cjenik (ne modulska konstanta)
    pdv = 1 + t['pdv']
    vt = (t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos'] + t['solidarna'] + t['oie']) * pdv
    nt = (t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos'] + t['solidarna'] + t['oie']) * pdv
    return vt * vt_udio + nt * (1 - vt_udio)


def collect(conn):
    from hepapp.tariff import get_tarifa, izracunaj_racun, get_vt_udio_registri
    out = {}
    sada = _now_utc().astimezone(LOCAL_TZ)   # lokalno vrijeme (HEP/SMA dnevni podaci su po lokalnom danu)

    # --- VT udio (registri) ---
    try:
        _, reg = get_vt_udio_registri(conn)
        vt_udio = reg if reg is not None else 0.45
    except Exception:
        vt_udio = 0.45
    out['vt_udio'] = round(vt_udio * 100, 1)

    # --- Solarni visak (zadnji sma_live) ---
    row = conn.execute(
        "SELECT ts, pv_generation_w, feed_in_w, external_consumption_w, total_consumption_w, autarky_rate "
        "FROM sma_live ORDER BY ts DESC LIMIT 1").fetchone()
    visak = None
    if row:
        # svjezina: samo ako je zadnji zapis < 30 min star
        try:
            dt = datetime.fromisoformat(row['ts'].replace('Z', '+00:00'))
            age_min = (_now_utc() - dt).total_seconds() / 60
        except Exception:
            age_min = 999
        # feed_in_w NULL (senzor nedostupan) nije 0 W — ostaje nepoznato
        if age_min < LIVE_MAX_AGE_MIN and row['feed_in_w'] is not None:
            visak = float(row['feed_in_w'])
    out['visak_w'] = round(visak, 0) if visak is not None else None
    # None = nepoznato (zastarjeli podaci) -> push() salje 'unavailable', ne 'off'
    out['isplati'] = (visak > VISAK_PRAG_W) if visak is not None else None

    # --- Dug (zadnji racun) ---
    d = conn.execute("SELECT dug FROM racuni WHERE dug IS NOT NULL ORDER BY period DESC LIMIT 1").fetchone()
    out['dug'] = round(d['dug'], 2) if d and d['dug'] is not None else None

    # --- Trosak struje ovaj mjesec do sada (HEP dnevna) ---
    mj = sada.strftime('%Y-%m')
    r = conn.execute("SELECT ROUND(SUM(kwh_plus),2) kp, ROUND(SUM(kwh_minus),2) km, COUNT(*) n "
                     "FROM ocitanja_dnevna WHERE substr(datum,1,7)=?", (mj,)).fetchone()
    if r and r['n']:
        out['trosak_mjesec'] = round(izracunaj_racun(r['kp'] or 0, r['km'] or 0, r['n'], vt_udio=vt_udio), 2)
    else:
        out['trosak_mjesec'] = None

    # --- Trosak danas + autarkija (SMA dnevna danas) ---
    dan = sada.strftime('%Y-%m-%d')
    s = conn.execute("SELECT grid_consumption_kwh g, feed_in_kwh f, total_consumption_kwh c, autarky_rate a "
                     "FROM sma_dnevna WHERE datum=?", (dan,)).fetchone()
    if s and (s['g'] is not None or s['f'] is not None):
        buy = _avg_buy_price(vt_udio)
        otkup = get_tarifa()['otkup']
        out['trosak_danas'] = round((s['g'] or 0) * buy - (s['f'] or 0) * otkup, 2)
        # autarkija: primarno iz grid/cons (pouzdano), pa spremljeni rate
        if s['c'] and s['c'] > 0 and s['g'] is not None:
            out['autarkija_danas'] = round(max(0, 1 - s['g'] / s['c']) * 100, 1)
        elif s['a'] is not None:
            out['autarkija_danas'] = round(s['a'] * 100, 1)
        else:
            out['autarkija_danas'] = None
    else:
        out['trosak_danas'] = None
        out['autarkija_danas'] = None

    # --- Po-inverterski dnevni prinos (danas) ---
    # NAPOMENA: pv_kwh_inv1/2 se NE osvjezavaju svakih 5 min kao ostatak reda —
    # puni ih tek dnevni SMA history/backfill, pa su tijekom dana tipicno prazni
    # ili od zadnjeg dnevnog uvoza (vidi atribut 'osvjezavanje' u push()).
    inv = conn.execute("SELECT pv_kwh_inv1 i1, pv_kwh_inv2 i2 FROM sma_dnevna WHERE datum=?",
                       (dan,)).fetchone()
    if inv and (inv['i1'] or inv['i2']):
        out['inv1_danas'] = round(inv['i1'] or 0, 2)
        out['inv2_danas'] = round(inv['i2'] or 0, 2)
    else:
        out['inv1_danas'] = None
        out['inv2_danas'] = None

    return out


def push(ha, vals):
    def s(eid, state, unit=None, name=None, icon=None, dclass=None, extra=None):
        # None = podatak zastario/nepoznat -> 'unavailable' (isti atributi), da
        # HA dashboardi i automatizacije ne gledaju zadnju staru vrijednost
        if state is None:
            state = UNAVAILABLE
        attrs = {}
        if unit:   attrs['unit_of_measurement'] = unit
        if name:   attrs['friendly_name'] = name
        if icon:   attrs['icon'] = icon
        if dclass: attrs['device_class'] = dclass
        if extra:  attrs.update(extra)
        ha.set_state(eid, state, attrs)

    s('sensor.energija_solarni_visak', vals.get('visak_w'), 'W', 'Solarni višak',
      'mdi:solar-power', 'power')
    isp = vals.get('isplati')
    ha.set_state('binary_sensor.energija_isplati_se_trositi',
                 UNAVAILABLE if isp is None else ('on' if isp else 'off'),
                 {'friendly_name': 'Isplati se trošiti', 'icon': 'mdi:flash',
                  'visak_prag_w': VISAK_PRAG_W})
    s('sensor.energija_dug', vals.get('dug'), 'EUR', 'Dug prema HEP-u', 'mdi:cash-minus', 'monetary')
    s('sensor.energija_trosak_mjesec', vals.get('trosak_mjesec'), 'EUR',
      'Trošak struje ovaj mjesec', 'mdi:calendar-month', 'monetary')
    s('sensor.energija_trosak_danas', vals.get('trosak_danas'), 'EUR',
      'Trošak struje danas', 'mdi:calendar-today', 'monetary')
    s('sensor.energija_vt_udio', vals.get('vt_udio'), '%', 'VT udio', 'mdi:chart-pie')
    s('sensor.energija_autarkija_danas', vals.get('autarkija_danas'), '%', 'Autarkija danas',
      'mdi:home-lightning-bolt')
    # izvor (sma_dnevna.pv_kwh_inv1/2) se puni samo jednom dnevno, ne svakih 5 min
    inv_extra = {'osvjezavanje': 'jednom dnevno (SMA history uvoz)'}
    s('sensor.energija_inv1_danas', vals.get('inv1_danas'), 'kWh', 'Inverter 1 danas',
      'mdi:solar-panel', 'energy', extra=inv_extra)
    s('sensor.energija_inv2_danas', vals.get('inv2_danas'), 'kWh', 'Inverter 2 danas',
      'mdi:solar-panel', 'energy', extra=inv_extra)


def main():
    url = os.environ.get('HA_URL', '').strip()
    token = os.environ.get('HA_TOKEN', '').strip()
    if not url or not token:
        log.error('HA_URL/HA_TOKEN nisu postavljeni'); return 1
    from ha_sender import HomeAssistantAPI
    ha = HomeAssistantAPI(url, token)
    if not ha.test_connection():
        log.error('HA nedostupan — preskacem push'); return 1
    conn = _conn()
    try:
        vals = collect(conn)
    finally:
        conn.close()
    log.info('vrijednosti: %s', vals)
    push(ha, vals)
    log.info('HA intel push gotov')
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
