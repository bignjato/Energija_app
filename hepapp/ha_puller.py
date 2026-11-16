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
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from netutil import ha_verify, retry_session

DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')

VERIFY  = ha_verify()
SESSION = retry_session()

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('ha_puller')

# HA dnevni senzori ("today") resetiraju se u LOKALNU ponoć — "danas" zato
# računamo eksplicitno u ovoj zoni, neovisno o TZ-u kontejnera.
LOCAL_TZ = ZoneInfo('Europe/Zagreb')

# kWh tolerancija kod usporedbe s jučerašnjim totalom (zaokruživanje senzora);
# jučerašnji total ispod ovoga se ne koristi kao dokaz "nije resetirano"
_RESET_TOL = 0.01


def _now_utc() -> datetime:
    """Trenutno vrijeme (aware, UTC). Izdvojeno radi testova (zamrznuti sat)."""
    return datetime.now(timezone.utc)


def _local_today() -> str:
    """Lokalni (Europe/Zagreb) kalendarski dan 'YYYY-MM-DD'."""
    return _now_utc().astimezone(LOCAL_TZ).strftime('%Y-%m-%d')


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
    """Vrati (value, unit) vrijednosti senzora u 00:00 danas (lokalna ponoć).

    HA history endpoint: GET /api/history/period/{start}?filter_entity_id=X
    Vraća listu intervala — prvi state je vrijednost na start vremenu.
    """
    if not entity_id:
        return None, None
    # LOKALNA ponoć danas (dnevni brojači se resetiraju po lokalnom vremenu),
    # pretvorena u UTC — HA očekuje ISO 8601 s timezone offsetom
    local_midnight = _now_utc().astimezone(LOCAL_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    today_midnight = local_midnight.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
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
    Ako ijedan entity iz liste nema vrijednost -> None (ne djelomična suma).
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
            log.warning('HA: %s nema vrijednost (sada ili u ponoć) — delta se ne računa '
                        '(bez djelomične sume)', eid)
            return None
        delta = now_v - mid_v
        delta_kwh = _normalize_energy(delta, now_u or mid_u)
        total = (total or 0) + delta_kwh
    return total


def _sum_entities(ha_url: str, token: str, entity_csv: str, energy: bool = False) -> float:
    """Sumiraj listu zarezom razdvojenih entity_id-eva (s normalizacijom unita).

    Ako je ijedan entity iz liste unavailable/unknown, vraća None — djelomična
    suma (npr. samo jedan od dva invertera) ne smije se spremiti kao total.
    """
    if not entity_csv:
        return None
    total = None
    for eid in entity_csv.split(','):
        eid = eid.strip()
        if not eid:
            continue
        v, unit = _get_state(ha_url, token, eid)
        if v is None:
            log.warning('HA: %s nedostupan — suma se odbacuje (bez djelomične sume)', eid)
            return None
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

    aut, aut_unit = _get_state(ha_url, token, os.environ.get('HA_ENT_AUTARKY'))
    if aut is not None:
        if (aut_unit or '').strip() == '%':
            aut = aut / 100.0          # 1 % -> 0.01 (ne 100 %)
        elif not aut_unit and aut > 1.5:
            # senzor bez unita: stara heuristika (0–100 vs 0–1)
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
        # sma_live.ts: UTC s eksplicitnim offsetom (isti format kao ostali writeri)
        _now_utc().strftime('%Y-%m-%dT%H:%M:%S+00:00'),
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

    # "Danas" se fiksira PRIJE čitanja senzora: ako čitanje (do 8 HTTP poziva s
    # timeoutima) prijeđe lokalnu ponoć, vrijednosti se ne smiju pripisati
    # novom danu — vidi provjeru nakon čitanja.
    today = _local_today()
    jucer = (datetime.strptime(today, '%Y-%m-%d') - timedelta(days=1)).strftime('%Y-%m-%d')

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

    if all(v is None for v in (pv_kwh, feed_kwh, cons_kwh, grid_kwh)):
        log.info('HA: nema dnevnih kWh senzora konfiguriranih.')
        return False

    if _local_today() != today:
        # čitanje je prešlo lokalnu ponoć: ne zna se kojem danu pripada koje
        # očitanje — preskoči, sljedeći pull (za 5 min) je čist
        log.info('HA today: lokalni dan se promijenio tijekom čitanja (%s) — preskačem upis', today)
        return True

    conn = sqlite3.connect(DB_PATH)
    kolone = ('SELECT pv_generation_kwh, feed_in_kwh, grid_consumption_kwh, total_consumption_kwh '
              'FROM sma_dnevna WHERE datum=?')
    old = conn.execute(kolone, (today,)).fetchone() or (None, None, None, None)
    yst = conn.execute(kolone, (jucer,)).fetchone() or (None, None, None, None)

    # Dnevni brojači unutar dana samo rastu: niže očitanje od već spremljenog
    # znači reset senzora ili krivi dan — ne smije pregaziti spremljeno.
    # Zato PRVI upis u danu mora biti ispravan (kasnije ga niže, točno očitanje
    # više ne može popraviti): dok za danas još nema vrijednosti, očitanje koje
    # je >= jučerašnjeg totala znači da se HA senzor još nije resetirao (reset
    # kasni par sekundi/minuta iza ponoći, ili PV senzor drži zadnju vrijednost
    # dok se inverter ujutro ne probudi) — ne sprema se. Pravo današnje
    # očitanje kreće od ~0 pa prolazi.
    # Vraća (vrijednost za upis, je li ovo očitanje prihvaćeno).
    def _mono(naziv, novo, staro, jucer_v):
        if novo is None:
            return staro, False
        if staro is None and jucer_v is not None and jucer_v > _RESET_TOL and novo >= jucer_v - _RESET_TOL:
            log.info('HA today %s: %s = %s kWh >= jučerašnjih %s — senzor se još nije resetirao, '
                     'ne spremam', today, naziv, novo, jucer_v)
            return None, False
        if staro is not None and novo < staro:
            log.warning('HA today %s: %s = %s kWh je niže od spremljenog %s — zadržavam spremljeno',
                        today, naziv, novo, staro)
            return staro, False
        return novo, True

    pv_kwh,   pv_ok   = _mono('pv',   pv_kwh,   old[0], yst[0])
    feed_kwh, feed_ok = _mono('feed', feed_kwh, old[1], yst[1])
    grid_kwh, grid_ok = _mono('grid', grid_kwh, old[2], yst[2])
    if cons_kwh is None and pv_ok and feed_ok and grid_ok:
        # Energy balance — samo iz očitanja prihvaćenih u OVOM pullu (ne iz
        # odbijenih/starih: npr. PV koji još drži jučerašnji total dao bi lažno
        # visoku potrošnju koja bi onda ostala zalijepljena cijeli dan)
        cons_kwh = max(0, pv_kwh - feed_kwh + grid_kwh)
    cons_kwh, _ = _mono('cons', cons_kwh, old[3], yst[3])

    if all(v is None for v in (pv_kwh, feed_kwh, cons_kwh, grid_kwh)):
        # sve odbijeno kao "još nije resetirano" — ne otvaraj prazan red
        conn.close()
        return True

    autarky = None
    if grid_kwh is not None and cons_kwh is not None and cons_kwh > 0:
        autarky = max(0.0, min(1.0, 1 - grid_kwh / cons_kwh))

    # Redoslijed placeholdera == redoslijed kolona (NE mijenjati jedno bez drugog):
    #   1 datum                 <- today      (lokalni dan)
    #   2 pv_generation_kwh     <- pv_kwh
    #   3 feed_in_kwh           <- feed_kwh
    #   4 grid_consumption_kwh  <- grid_kwh   (preuzeto iz mreže)
    #   5 total_consumption_kwh <- cons_kwh   (ukupna potrošnja kuće)
    #   6 autarky_rate          <- autarky    (0–1)
    # MAX u UPDATE-u je ista "samo raste" zaštita na razini SQL-a (ako je drugi
    # writer upisao red između SELECT-a i INSERT-a).
    conn.execute('''
        INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, grid_consumption_kwh,
                                total_consumption_kwh, autarky_rate)
        VALUES (?,?,?,?,?,?)
        ON CONFLICT(datum) DO UPDATE SET
            pv_generation_kwh    = COALESCE(MAX(excluded.pv_generation_kwh, sma_dnevna.pv_generation_kwh),
                                            excluded.pv_generation_kwh, sma_dnevna.pv_generation_kwh),
            feed_in_kwh          = COALESCE(MAX(excluded.feed_in_kwh, sma_dnevna.feed_in_kwh),
                                            excluded.feed_in_kwh, sma_dnevna.feed_in_kwh),
            grid_consumption_kwh = COALESCE(MAX(excluded.grid_consumption_kwh, sma_dnevna.grid_consumption_kwh),
                                            excluded.grid_consumption_kwh, sma_dnevna.grid_consumption_kwh),
            total_consumption_kwh= COALESCE(MAX(excluded.total_consumption_kwh, sma_dnevna.total_consumption_kwh),
                                            excluded.total_consumption_kwh, sma_dnevna.total_consumption_kwh),
            autarky_rate         = COALESCE(excluded.autarky_rate, sma_dnevna.autarky_rate)
    ''', (today, pv_kwh, feed_kwh, grid_kwh, cons_kwh, autarky))
    conn.commit()
    conn.close()
    log.info('HA pull today %s: PV=%s kWh, feed=%s kWh, cons=%s kWh, grid=%s kWh, aut=%s',
             today, pv_kwh, feed_kwh, cons_kwh, grid_kwh, autarky)
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
