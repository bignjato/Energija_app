#!/usr/bin/env python3
"""Odrzavanje baze: backfill sma_dnevna flow-metrika, retencija sma_live,
normalizacija timestampa, backfill rupa u SMA povijesti, alert na stale sync.

Pokretanje:
  python /app/maintenance.py --all
  python /app/maintenance.py --backfill-daily
  python /app/maintenance.py --prune-live --keep-days 30
  python /app/maintenance.py --normalize-ts
  python /app/maintenance.py --backfill-range 2026-01-05 2026-01-14
  python /app/maintenance.py --check-freshness
  python /app/maintenance.py --check-anomaly
  python /app/maintenance.py --bill-reminder
  python /app/maintenance.py --restore-drill
  python /app/maintenance.py --check-offsite
  python /app/maintenance.py --check-token-age
  python /app/maintenance.py --check-inverter
"""
import os, sqlite3, logging, argparse, sys, json, time
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('maintenance')

DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')


def _conn():
    # timeout=30: web/sync kontejner dijele bazu — cekaj lock umjesto "database is locked"
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    return c


def _lokalna_zona():
    """Europe/Zagreb; ako tzdata fali, sistemska zona (kontejneri imaju TZ=Europe/Zagreb)."""
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo('Europe/Zagreb')
    except Exception:
        return None


def _u_lokalno(dt):
    """UTC/aware datetime -> lokalno (Zagreb) vrijeme. Naivni ts tretiraj kao UTC."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_lokalna_zona())


def _now_ts():
    """Epoch sekunde (izdvojeno radi testova)."""
    return time.time()


# ---------- 1. Backfill sma_dnevna flow-metrika iz sma_live ----------
def backfill_daily(conn):
    """Dnevne feed/grid/cons kWh + autarkija iz sma_live (W uzorci).

    Integracija: trapezno po danu (W × dt). Samo dani gdje sma_dnevna jos
    nema flow podatke (feed_in_kwh IS NULL). PV ostaje iz sma_15min importa.
    sma_live.ts je UTC, a sma_dnevna.datum LOKALNI dan — uzorci se grupiraju po
    lokalnom (Zagreb) danu. Danasnji dan se preskace: integracija nepotpunog
    dana bi (zbog COALESCE) zamrznula djelomican zbroj; danasnji red puni HA puller.
    """
    rows = conn.execute('''
        SELECT ts, pv_generation_w, feed_in_w, external_consumption_w,
               total_consumption_w, autarky_rate
        FROM sma_live
        WHERE feed_in_w IS NOT NULL
        ORDER BY ts
    ''').fetchall()
    if not rows:
        log.info('backfill_daily: nema sma_live flow podataka')
        return 0

    # grupiraj po danu
    po_danu = {}
    for r in rows:
        ts = r['ts']
        try:
            dt = datetime.fromisoformat(ts.replace('Z', '+00:00'))
        except Exception:
            continue
        if dt.tzinfo is None:              # stari zapisi bez offseta su UTC
            dt = dt.replace(tzinfo=timezone.utc)
        po_danu.setdefault(_u_lokalno(dt).date().isoformat(), []).append((dt, r))

    # koje dane treba (sma_dnevna.feed_in_kwh IS NULL ili red ne postoji);
    # samo zavrseni lokalni dani
    danas = _u_lokalno(datetime.now(timezone.utc)).date().isoformat()
    treba = set()
    for d in po_danu:
        if d >= danas:
            continue
        row = conn.execute('SELECT feed_in_kwh FROM sma_dnevna WHERE datum=?', (d,)).fetchone()
        if row is None or row['feed_in_kwh'] is None:
            treba.add(d)

    def integr(samples, key):
        """kWh = Σ W·dt / 1000 / 3600 (trapezno)."""
        pts = [(dt, getattr_w(r, key)) for dt, r in samples]
        pts = [(dt, w) for dt, w in pts if w is not None]
        if len(pts) < 2:
            return None
        wh = 0.0
        for (t0, w0), (t1, w1) in zip(pts, pts[1:]):
            dt_s = (t1 - t0).total_seconds()
            if 0 < dt_s <= 1800:           # preskoci rupe > 30 min
                wh += (w0 + w1) / 2.0 * dt_s
        return round(wh / 3_600_000.0, 2)  # Ws → kWh

    n = 0
    for d in sorted(treba):
        s = sorted(po_danu[d], key=lambda x: x[0])
        pv   = integr(s, 'pv_generation_w')
        feed = integr(s, 'feed_in_w')
        grid = integr(s, 'external_consumption_w')
        cons = integr(s, 'total_consumption_w')
        aut  = None
        if cons and cons > 0 and grid is not None:
            aut = round(max(0.0, min(1.0, 1 - grid / cons)), 4)
        conn.execute('''
            INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh,
                                    grid_consumption_kwh, total_consumption_kwh, autarky_rate)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(datum) DO UPDATE SET
                feed_in_kwh           = COALESCE(sma_dnevna.feed_in_kwh, excluded.feed_in_kwh),
                grid_consumption_kwh  = COALESCE(sma_dnevna.grid_consumption_kwh, excluded.grid_consumption_kwh),
                total_consumption_kwh = COALESCE(sma_dnevna.total_consumption_kwh, excluded.total_consumption_kwh),
                autarky_rate          = COALESCE(sma_dnevna.autarky_rate, excluded.autarky_rate),
                pv_generation_kwh     = COALESCE(sma_dnevna.pv_generation_kwh, excluded.pv_generation_kwh)
        ''', (d, pv, feed, grid, cons, aut))
        n += 1
    conn.commit()
    log.info('backfill_daily: %d dana azurirano (od %s)', n, min(treba) if treba else '-')
    return n


def getattr_w(row, key):
    v = row[key]
    return float(v) if v is not None else None


# ---------- 4a. Normalizacija sma_live timestampa ----------
def normalize_ts(conn):
    """Ujednaci sma_live.ts na '...+00:00' (neki stari zapisi bez tz offseta)."""
    rows = conn.execute("SELECT rowid AS rid, ts FROM sma_live WHERE ts NOT LIKE '%+00:00' AND ts NOT LIKE '%Z'").fetchall()
    n = 0
    for r in rows:
        ts = r['ts']
        new = ts.rstrip('Z')
        if '+' not in new[10:]:
            new = new + '+00:00'
        if new != ts:
            conn.execute('UPDATE sma_live SET ts=? WHERE rowid=?', (new, r['rid']))
            n += 1
    conn.commit()
    log.info('normalize_ts: %d zapisa normalizirano', n)
    return n


# ---------- 4b. Retencija sma_live ----------
def prune_live(conn, keep_days=30):
    """Brisi sma_live starije od keep_days (5-min uzorci, samo za 'danas' view)."""
    prije = conn.execute('SELECT COUNT(*) c FROM sma_live').fetchone()['c']
    # ts je 'YYYY-MM-DDTHH:MM:SS+00:00' (UTC) — granica u istom obliku ('T', ne razmak)
    conn.execute("DELETE FROM sma_live WHERE ts < strftime('%Y-%m-%dT%H:%M:%S', 'now', ?)",
                 (f'-{keep_days} days',))
    conn.commit()
    poslije = conn.execute('SELECT COUNT(*) c FROM sma_live').fetchone()['c']
    log.info('prune_live: obrisano %d zapisa (zadrzano %d, %d dana)', prije - poslije, poslije, keep_days)
    return prije - poslije


# ---------- 5. Backfill SMA rupe (PV iz SMA API) ----------
def backfill_range(conn, od, do):
    """Re-import sma_15min PV za zadani raspon (popravlja rupe u povijesti)."""
    try:
        from sma_history_import import get_token, fetch_week
    except Exception as e:
        log.error('backfill_range: ne mogu uvesti sma_history_import: %s', e)
        return 0
    start = datetime.fromisoformat(od).replace(tzinfo=timezone.utc)
    end   = datetime.fromisoformat(do).replace(tzinfo=timezone.utc) + timedelta(days=1)
    token = get_token()
    total = 0
    cur = start
    while cur < end:
        we = min(cur + timedelta(days=7), end)
        inv1, inv2 = fetch_week(token,
                                cur.strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                                we.strftime('%Y-%m-%dT%H:%M:%S.000Z'))
        for ts in set(inv1) | set(inv2):
            w1, w2 = inv1.get(ts, 0), inv2.get(ts, 0)
            conn.execute('INSERT OR REPLACE INTO sma_15min (ts,pv_w_inv1,pv_w_inv2,pv_w_total) VALUES (?,?,?,?)',
                         (ts, w1, w2, w1 + w2))
            total += 1
        conn.commit()
        cur = we
    _agregiraj_pv_dnevno(conn, od, do)
    conn.commit()
    log.info('backfill_range %s..%s: %d 15-min zapisa', od, do, total)
    return total


def _agregiraj_pv_dnevno(conn, od, do):
    """sma_15min (UTC 'Z') -> sma_dnevna PV stupci za lokalne dane od..do.

    Upsert dira SAMO PV stupce — INSERT OR REPLACE bi obrisao feed_in/grid/
    total_consumption/autarky postojecih redaka.
    """
    conn.execute('''
        INSERT INTO sma_dnevna (datum, pv_generation_kwh, pv_kwh_inv1, pv_kwh_inv2)
        SELECT date(ts,'localtime'), ROUND(SUM(pv_w_total)*0.25/1000.0,3),
               ROUND(SUM(pv_w_inv1)*0.25/1000.0,3), ROUND(SUM(pv_w_inv2)*0.25/1000.0,3)
        FROM sma_15min WHERE pv_w_total > 0 AND date(ts,'localtime') BETWEEN ? AND ?
        GROUP BY date(ts,'localtime')
        ON CONFLICT(datum) DO UPDATE SET
            pv_generation_kwh = excluded.pv_generation_kwh,
            pv_kwh_inv1       = excluded.pv_kwh_inv1,
            pv_kwh_inv2       = excluded.pv_kwh_inv2
    ''', (od, do))


# ---------- HA notifikacije ----------
def _ha_send(title, message):
    """Posalji HA notifikaciju. Vraca True ako je poslana."""
    import requests
    from netutil import ha_verify
    ha_url = os.environ.get('HA_URL', '').strip().rstrip('/')
    token  = os.environ.get('HA_TOKEN', '').strip()
    notify = os.environ.get('HA_NOTIFY_SERVICE', 'notify/persistent_notification')
    if not ha_url or not token:
        log.info('_ha_send: HA nije konfiguriran — preskacem')
        return False
    try:
        r = requests.post(f'{ha_url}/api/services/{notify}',
                          headers={'Authorization': f'Bearer {token}'},
                          json={'title': title, 'message': message},
                          timeout=15, verify=ha_verify())
        log.info('_ha_send: %s -> %s', notify, r.status_code)
        return r.ok
    except Exception as e:
        # samo tip iznimke — tekst requests iznimke sadrzi URL
        log.warning('_ha_send greska: %s', type(e).__name__)
        return False


_TG_MD_ZNAKOVI = '_*`['


def _tg_escape(tekst):
    """Escape znakova koje Telegramov (legacy) Markdown tumaci kao pocetak
    entiteta. Neuparen '_' (npr. ime tablice ili backup datoteke u poruci)
    inace daje HTTP 400 "can't parse entities" i poruka nikad ne stigne.
    """
    return ''.join('\\' + ch if ch in _TG_MD_ZNAKOVI else ch for ch in str(tekst))


def _tg_send(title, message):
    """Posalji Telegram poruku (neovisno o HA). Vraca True ako je poslana.

    Kredencijali iz env-a: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
    Poruka se escapea za Markdown; ako Telegram ipak odbije formatiranje
    (HTTP 400), salje se jos jednom kao obican tekst — alert je vazniji od
    podebljanog naslova.
    """
    import requests
    tok  = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
    chat = os.environ.get('TELEGRAM_CHAT_ID', '').strip()
    if not tok or not chat:
        log.info('_tg_send: TELEGRAM_BOT_TOKEN/CHAT_ID nisu postavljeni — preskacem')
        return False
    # unutar *...* escape ne vrijedi -> iz naslova se Markdown znakovi izbacuju
    naslov = ''.join(ch for ch in str(title) if ch not in _TG_MD_ZNAKOVI)
    pokusaji = [
        {'text': f'\u26a1 *{naslov}*\n{_tg_escape(message)}', 'parse_mode': 'Markdown'},
        {'text': f'\u26a1 {title}\n{message}'},        # fallback: obican tekst
    ]
    try:
        for i, p in enumerate(pokusaji):
            r = requests.post(f'https://api.telegram.org/bot{tok}/sendMessage',
                              json={'chat_id': chat, 'disable_web_page_preview': True, **p},
                              timeout=15)
            log.info('_tg_send: -> %s', r.status_code)
            if r.ok:
                return True
            log.warning('_tg_send body: %s', r.text[:150])
            if r.status_code != 400 or i == len(pokusaji) - 1:
                return False
            log.warning('_tg_send: Markdown odbijen — ponavljam kao obican tekst')
        return False
    except Exception as e:
        # NIKAD ne logiraj samu iznimku: njen tekst sadrzi URL s bot tokenom
        resp = getattr(e, 'response', None)
        status = getattr(resp, 'status_code', None)
        if status is not None:
            log.warning('_tg_send greska: %s (HTTP %s)', type(e).__name__, status)
        else:
            log.warning('_tg_send greska: %s', type(e).__name__)
        return False


def ha_notify(title, message):
    """Fan-out alert na SVE dostupne kanale (HA + Telegram).

    Ime zadrzano radi kompatibilnosti sa svim pozivateljima. Telegram je
    neovisan o HA-u pa alert stize i kad je HA server dolje. Vraca True ako
    je barem jedan kanal uspio.
    """
    ha = _ha_send(title, message)
    tg = _tg_send(title, message)
    if not (ha or tg):
        log.warning('ha_notify: nijedan kanal nije dostupan (HA+Telegram)')
    return ha or tg


def _get_cfg(conn, key, default=''):
    try:
        row = conn.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
        return row['value'] if row else default
    except sqlite3.Error:
        return default


def _set_cfg(conn, key, value):
    conn.execute('CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT, '
                 "updated TEXT DEFAULT (datetime('now')))")
    conn.execute("INSERT OR REPLACE INTO config (key, value, updated) VALUES (?, ?, datetime('now'))",
                 (key, value))
    conn.commit()


# ---------- 2. Alert na stale sync (preko HA) ----------
FRESHNESS_STATE_KEY = '_freshness_state'
FRESHNESS_RENOTIFY_S = 24 * 3600


def _hep_zadnji_uzorak(conn):
    """Lokalni ts ('YYYY-MM-DDTHH:MM:SS') kraja zadnjeg HEP intervala sa
    stvarnim podacima, ili None ako podataka nema.

    Nula-uzorci (0 kWh u oba smjera) su HEP-ova popuna za jos neisporuceno
    razdoblje pa se ne broje. Ako ocitanja_15min nema nista, uzima se kraj
    zadnjeg dana iz ocitanja_dnevna (dan D zavrsava uzorkom D+1 00:00:00).
    """
    def _max(sql):
        try:
            return conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError as e:
            if 'no such table' not in str(e):
                raise
            return None

    ts = _max('SELECT MAX(ts) FROM ocitanja_15min WHERE kwh_plus > 0 OR kwh_minus > 0')
    if ts:
        return ts
    dan = _max('SELECT MAX(datum) FROM ocitanja_dnevna')
    if not dan:
        return None
    try:
        return (datetime.fromisoformat(dan[:10]) + timedelta(days=1)).strftime('%Y-%m-%dT%H:%M:%S')
    except ValueError:
        return dan      # necitljivo -> javi pozivatelj


def _freshness_problemi(conn, max_hep_h, max_sma_h, max_sync_h):
    """Vrati {kljuc: opis} aktivnih problema ('hep', 'sma', 'sync')."""
    problemi = {}

    # (a) HEP: starost zadnjeg STVARNOG 15-min uzorka u satima. Kalendarska
    # usporedba MAX(datum) s danasnjim danom nema marginu: HEP dan D isporucuje
    # tek tijekom D+1, pa bi svake noci (od ponoci do isporuke) zadnji dan bio
    # "2 dana iza" => lazni alert + "rijeseno" svako jutro.
    zadnji = _hep_zadnji_uzorak(conn)
    if zadnji is None:
        problemi['hep'] = 'HEP podaci: nema nijednog ocitanja'
    else:
        try:
            # ts je LOKALNO naivno vrijeme (kraj intervala) kao i datetime.now()
            h = (datetime.now() - datetime.fromisoformat(zadnji[:19])).total_seconds() / 3600
            if h >= max_hep_h:
                problemi['hep'] = f'HEP podaci kasne {h:.0f} h (zadnji uzorak {zadnji})'
        except ValueError:
            problemi['hep'] = f'HEP podaci: necitljiv zadnji ts ({zadnji})'

    # (b) SMA live: ts je UTC; zapis bez offseta tretiraj kao UTC (ne preskaci provjeru)
    try:
        sma = conn.execute('SELECT MAX(ts) t FROM sma_live').fetchone()['t']
    except sqlite3.OperationalError as e:
        if 'no such table' not in str(e):
            raise
        sma = None
    if not sma:
        problemi['sma'] = 'SMA live: nema nijednog uzorka'
    else:
        try:
            dt = datetime.fromisoformat(sma.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            if h >= max_sma_h:
                problemi['sma'] = f'SMA live kasni {h:.0f} h (zadnji {sma})'
        except ValueError:
            problemi['sma'] = f'SMA live: necitljiv zadnji ts ({sma})'

    # (c) HEP sync pada SADA: ima ERROR zapisa, a zadnji OK je stariji od
    # max_sync_h (ili ga nema). Kratki ispad koji se sam oporavio (OK nakon
    # gresaka) se NE javlja. sync_log.ts je UTC 'YYYY-MM-DD HH:MM:SS' kao i
    # datetime('now') -> usporedba istog formata.
    try:
        r = conn.execute("""
            SELECT (SELECT COUNT(*) FROM sync_log WHERE status='ERROR') n_err,
                   (SELECT MAX(ts) FROM sync_log WHERE status='OK') zadnji_ok,
                   (SELECT COUNT(*) FROM sync_log WHERE status='OK'
                      AND ts >= datetime('now', ?)) n_ok_svjezih
        """, (f'-{float(max_sync_h)} hours',)).fetchone()
    except sqlite3.OperationalError as e:
        if 'no such table' not in str(e):
            raise
        r = None
    if r and r['n_err'] and not r['n_ok_svjezih']:
        if r['zadnji_ok']:
            n = conn.execute("SELECT COUNT(*) c FROM sync_log WHERE status='ERROR' AND ts > ?",
                             (r['zadnji_ok'],)).fetchone()['c']
            problemi['sync'] = (f'HEP sync ne uspijeva: zadnji uspjesan {r["zadnji_ok"]} UTC, '
                                f'{n} gresaka od tada')
        else:
            problemi['sync'] = f'HEP sync ne uspijeva: {r["n_err"]} gresaka, nijedan uspjesan sync'
    return problemi


def check_freshness(conn, max_hep_h=48, max_sma_h=12, max_sync_h=6):
    """Alert ako HEP/SMA podaci kasne ili HEP sync trenutno pada.

    Stanje (skup javljenih problema + vrijeme zadnje poruke) se cuva u config
    (_freshness_state) da alert ne stize svaki sat dok ispad traje:
      - novi problem (kojeg nema u zadnjoj poruci) -> javi odmah
      - isti problemi traju -> ponovi najvise jednom u 24 h
      - sve opet zdravo nakon alerta -> jedna kratka poruka "rijeseno"
    Stanje se sprema tek nakon USPJESNOG slanja (inace bi se alert izgubio).
    Vraca True ako ima aktivnih problema.
    """
    problemi = _freshness_problemi(conn, max_hep_h, max_sma_h, max_sync_h)
    try:
        st = json.loads(_get_cfg(conn, FRESHNESS_STATE_KEY) or '{}')
        javljeni = set(st.get('problemi') or [])
        zadnja = float(st.get('ts') or 0)
    except (ValueError, TypeError, AttributeError):
        javljeni, zadnja = set(), 0.0
    sada = _now_ts()

    def spremi(kljucevi, ts):
        _set_cfg(conn, FRESHNESS_STATE_KEY,
                 json.dumps({'problemi': sorted(kljucevi), 'ts': ts}))

    if not problemi:
        if javljeni:
            log.info('check_freshness: problemi rijeseni (%s)', ', '.join(sorted(javljeni)))
            if ha_notify('Energetski Monitor — rijeseno',
                         'Energetski Monitor: podaci ponovno stizu uredno.'):
                spremi([], sada)
        else:
            log.info('check_freshness: OK')
        return False

    aktivni = set(problemi)
    poruka = 'Energetski Monitor — problemi:\n• ' + '\n• '.join(problemi[k] for k in sorted(problemi))
    log.warning(poruka.replace('\n', ' | '))
    novi = aktivni - javljeni
    if novi or (sada - zadnja) >= FRESHNESS_RENOTIFY_S:
        if ha_notify('Energetski Monitor — alert', poruka):
            spremi(aktivni, sada)
    else:
        log.info('check_freshness: vec javljeno (ponavljanje najvise jednom u 24 h)')
        if aktivni != javljeni:
            # dio problema je nestao: suzi skup (bez poruke) da se povratak
            # tog problema javi kao novi; vrijeme zadnje poruke ostaje
            spremi(aktivni, zadnja)
    return True


# ---------- 3. Anomaly detection (potrosnja iznad prosjeka) ----------
def check_anomaly(conn, threshold_perc=None, min_samples=3, min_avg_kwh=1.0,
                  min_abs_kwh=None):
    """Alarm ako potrosnja zadnjeg kompletnog dana znacajno odskace.

    Da se izbjegnu sezonski lazni alarmi (npr. pocetak grijanja digne SVE dane),
    okida samo ako je dan iznad praga u OBA referentna okvira:
      - same-weekday: prosjek istog dana u tjednu (zadnjih 8 tjedana)
      - rolling-14:   prosjek bilo kojeg dana zadnjih 14 dana
    Postupni sezonski rast digne i rolling-14 prosjek pa razlika nestaje;
    pravi jednodnevni skok probija oba. Uz to mora preci apsolutni kWh prag
    (ANOMALY_MIN_ABS_KWH, default 3) da se filtrira sum na malim brojkama.
    Notifikacija max jednom po datumu. Ocjenjuje se samo dan za koji
    ocitanja_15min ima cijeli dan; nepotpun dan se preskace BEZ markera pa se
    provjeri kasnije, kad HEP isporuci ostatak.
    """
    if threshold_perc is None:
        try:
            threshold_perc = float(os.environ.get('ANOMALY_THRESHOLD_PERC', '50'))
        except ValueError:
            threshold_perc = 50.0
    if min_abs_kwh is None:
        try:
            min_abs_kwh = float(os.environ.get('ANOMALY_MIN_ABS_KWH', '3'))
        except ValueError:
            min_abs_kwh = 3.0

    kandidati = conn.execute(
        'SELECT datum, ROUND(SUM(kwh_plus),2) kp FROM ocitanja_dnevna '
        'GROUP BY datum ORDER BY datum DESC LIMIT 7').fetchall()
    if not kandidati:
        log.info('check_anomaly: nema dnevnih podataka')
        return False
    # najnoviji KOMPLETAN dan (najnoviji red je cesto tek djelomicno isporucen)
    zadnji = next((r for r in kandidati if _dan_kompletan(conn, r['datum'])), None)
    if zadnji is None:
        log.info('check_anomaly: %s jos nije kompletan — preskacem (bez markera)',
                 kandidati[0]['datum'])
        return False
    if not zadnji['kp']:
        log.info('check_anomaly: nema dnevnih podataka')
        return False
    datum, kp = zadnji['datum'], zadnji['kp']

    if _get_cfg(conn, '_anomaly_last_date') >= datum:
        log.info('check_anomaly: %s vec provjeren', datum)
        return False

    wd = conn.execute('''
        SELECT AVG(kp) avg_kp, COUNT(*) n FROM (
            SELECT datum, SUM(kwh_plus) kp FROM ocitanja_dnevna
            WHERE datum < ? AND datum >= date(?, '-56 days')
              AND strftime('%w', datum) = strftime('%w', ?)
            GROUP BY datum
        )
    ''', (datum, datum, datum)).fetchone()
    roll = conn.execute('''
        SELECT AVG(kp) avg_kp, COUNT(*) n FROM (
            SELECT datum, SUM(kwh_plus) kp FROM ocitanja_dnevna
            WHERE datum < ? AND datum >= date(?, '-14 days')
            GROUP BY datum
        )
    ''', (datum, datum)).fetchone()

    if not wd or (wd['n'] or 0) < min_samples or (wd['avg_kp'] or 0) < min_avg_kwh:
        log.info('check_anomaly: premalo referentnih dana (%s)', wd['n'] if wd else 0)
        _set_cfg(conn, '_anomaly_last_date', datum)
        return False
    avg_wd = wd['avg_kp']
    avg_roll = roll['avg_kp'] if roll and (roll['n'] or 0) >= min_samples else avg_wd
    odst_wd   = 100.0 * (kp - avg_wd) / avg_wd
    odst_roll = 100.0 * (kp - avg_roll) / avg_roll if avg_roll else 0
    log.info('check_anomaly: %s %.1f kWh | same-weekday %.1f (%+.0f%%) | rolling14 %.1f (%+.0f%%) | prag +%.0f%%, min abs %.1f',
             datum, kp, avg_wd, odst_wd, avg_roll, odst_roll, threshold_perc, min_abs_kwh)
    # Mora probiti prag u OBA okvira + apsolutni minimum nad oba prosjeka
    if (odst_wd < threshold_perc or odst_roll < threshold_perc
            or (kp - avg_wd) < min_abs_kwh or (kp - avg_roll) < min_abs_kwh):
        _set_cfg(conn, '_anomaly_last_date', datum)
        return False
    # marker tek nakon uspjesnog slanja — inace se alert izgubi kad su kanali dolje
    if not ha_notify('Energetski Monitor — anomalija potrosnje',
                     f'{datum}: potrosnja {kp:.1f} kWh je {odst_wd:.0f}% iznad prosjeka za taj dan u '
                     f'tjednu ({avg_wd:.1f} kWh) i {odst_roll:.0f}% iznad prosjeka zadnjih 14 dana ({avg_roll:.1f} kWh).'):
        return False
    _set_cfg(conn, '_anomaly_last_date', datum)
    return True


def _dan_kompletan(conn, datum):
    """Ima li ocitanja_15min cijeli dan `datum` za svako mjerno mjesto tog dana.

    HEP uzorak nosi oznaku KRAJA intervala: dan D cine uzorci D 00:15:00 ..
    D+1 00:00:00. Normalno 96; prihvaca se 92-100 zbog dana s pomakom sata.
    """
    try:
        sutra = (datetime.fromisoformat(datum) + timedelta(days=1)).strftime('%Y-%m-%d')
        rows = conn.execute('''
            SELECT d.mjerno_mjesto m,
                   (SELECT COUNT(*) FROM ocitanja_15min q
                     WHERE q.mjerno_mjesto = d.mjerno_mjesto
                       AND q.ts > ? AND q.ts <= ?) n
            FROM ocitanja_dnevna d WHERE d.datum = ?
            GROUP BY d.mjerno_mjesto
        ''', (f'{datum}T00:00:00', f'{sutra}T00:00:00', datum)).fetchall()
    except (sqlite3.OperationalError, ValueError) as e:
        if isinstance(e, sqlite3.OperationalError) and 'no such table' not in str(e):
            raise
        return False
    return bool(rows) and all(92 <= r['n'] <= 100 for r in rows)


# ---------- 6. Podsjetnik za upload HEP racuna ----------
def bill_reminder(conn, od_dana=10):
    """Nakon `od_dana`. u mjesecu podsjeti (jednom mjesecno) ako racun za
    prethodni mjesec jos nije unesen — vise racuna = bolja projekcija duga.
    """
    now = datetime.now()
    if now.day < od_dana:
        return False
    prvi_ovaj_mj = now.replace(day=1)
    prosli = prvi_ovaj_mj - timedelta(days=1)
    period = f'{prosli.month:02d}/{prosli.year}'       # prikaz u poruci
    # racuni.period je 'YYYY-MM' (dashboard/racuni.py normaliziraju prije
    # spremanja); stari 'MM/YYYY' se i dalje prihvaca
    period_iso = f'{prosli.year}-{prosli.month:02d}'

    ovaj_mj = now.strftime('%Y-%m')
    if _get_cfg(conn, '_bill_reminder_last') == ovaj_mj:
        return False
    try:
        ima = conn.execute('SELECT 1 FROM racuni WHERE period IN (?, ?)',
                           (period_iso, period)).fetchone()
    except sqlite3.Error:
        return False   # tablica jos ne postoji (svjezа instalacija)
    if ima:
        return False
    if not ha_notify('Energetski Monitor — racun',
                     f'HEP racun za {period} jos nije unesen. Uploadaj PDF u Racuni tab — '
                     'projekcija duga je tocnija sa svakim racunom.'):
        return False   # nije poslano — bez markera, pokusaj opet sutra
    _set_cfg(conn, '_bill_reminder_last', ovaj_mj)
    log.info('bill_reminder: poslan podsjetnik za %s', period)
    return True


# ---------- 7. Restore drill (provjera da je backup vracljiv) ----------
BACKUP_DIR = os.environ.get('BACKUP_DIR', '/data/backups')
OFFSITE_MARKER = os.environ.get('OFFSITE_MARKER', '/data/.last_offsite_ok')
DRILL_KEY_TABLES = ('config', 'racuni', 'ocitanja_dnevna')


def restore_drill(conn, every_days=30):
    """Otvori najnoviji backup, integrity_check + provjera kljucnih tablica.

    Backup koji nitko nije probao vratiti nije backup. Pokrece se ~mjesecno;
    cadence se cuva u config (_restore_drill_last). Alert na neuspjeh.
    """
    last = _get_cfg(conn, '_restore_drill_last')
    today = datetime.now().strftime('%Y-%m-%d')
    if last:
        try:
            if (datetime.fromisoformat(today) - datetime.fromisoformat(last)).days < every_days:
                log.info('restore_drill: preskacem (zadnji %s)', last)
                return False
        except ValueError:
            pass

    import glob
    backupi = sorted(glob.glob(os.path.join(BACKUP_DIR, 'hep_energy_*.db')))
    if not backupi:
        log.warning('restore_drill: nema backupa u %s', BACKUP_DIR)
        ha_notify('Energetski Monitor — backup',
                  f'Restore drill: nema nijednog backupa u {BACKUP_DIR}!')
        return False
    latest = backupi[-1]
    problemi = []
    try:
        b = sqlite3.connect(f'file:{latest}?mode=ro', uri=True)
        b.row_factory = sqlite3.Row
        ic = b.execute('PRAGMA integrity_check').fetchone()[0]
        if ic != 'ok':
            problemi.append(f'integrity_check: {ic}')
        tablice = {r['name'] for r in b.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for t in DRILL_KEY_TABLES:
            if t not in tablice:
                problemi.append(f'nedostaje tablica {t}')
            else:
                n = b.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
                log.info('restore_drill: %s = %d redaka', t, n)
        b.close()
    except sqlite3.Error as e:
        problemi.append(f'ne mogu otvoriti backup: {e}')

    if problemi:
        poruka = f'Restore drill NEUSPJESAN ({os.path.basename(latest)}):\n• ' + '\n• '.join(problemi)
        log.error(poruka.replace('\n', ' | '))
        # cadence marker samo ako je alert stigao — inace ponovi drill sutra
        if ha_notify('Energetski Monitor — backup PROBLEM', poruka):
            _set_cfg(conn, '_restore_drill_last', today)
        return False
    _set_cfg(conn, '_restore_drill_last', today)
    log.info('restore_drill: OK (%s vracljiv)', os.path.basename(latest))
    return True


# ---------- 8. Off-site backup health ----------
def check_offsite(conn, max_age_days=2, remind_disabled_days=30):
    """Alert ako je off-site ukljucen ali zadnji upload zastario; ili podsjeti
    (rijetko) ako je off-site iskljucen pa backupi zive samo na istom volumenu.
    """
    mode = (os.environ.get('OFFSITE_BACKUP_MODE', 'disabled') or 'disabled').strip().lower()
    today = datetime.now().strftime('%Y-%m-%d')

    if mode == 'disabled' or not os.environ.get('OFFSITE_BACKUP_DEST', '').strip():
        last_warn = _get_cfg(conn, '_offsite_warn_last')
        if last_warn:
            try:
                if (datetime.fromisoformat(today) - datetime.fromisoformat(last_warn)).days < remind_disabled_days:
                    return False
            except ValueError:
                pass
        log.warning('check_offsite: off-site iskljucen — backupi su samo na lokalnom volumenu')
        if ha_notify('Energetski Monitor — backup',
                     'Off-site backup je iskljucen. Backupi baze zive samo na istom volumenu '
                     'kao i baza — gubitak volumena znaci gubitak svega. Postavi OFFSITE_BACKUP_MODE.'):
            _set_cfg(conn, '_offsite_warn_last', today)
        return False

    # ukljucen — provjeri marker zadnjeg uspjesnog uploada (pise offsite_backup.sh)
    try:
        with open(OFFSITE_MARKER) as f:
            zadnji = f.read().strip()
    except OSError:
        zadnji = None
    if not zadnji:
        log.info('check_offsite: nema markera uspjeha (jos nije bilo uploada?)')
        return False
    try:
        dana = (datetime.fromisoformat(today) - datetime.fromisoformat(zadnji[:10])).days
    except ValueError:
        return False
    if dana > max_age_days:
        log.warning('check_offsite: zadnji off-site upload prije %d dana (%s)', dana, zadnji)
        ha_notify('Energetski Monitor — backup',
                  f'Off-site backup kasni {dana} dana (zadnji uspjesan: {zadnji}). Provjeri rclone/rsync.')
        return True
    log.info('check_offsite: OK (zadnji upload %s)', zadnji)
    return False


# ---------- 9. Podsjetnik za rotaciju tokena/lozinki ----------
def check_token_age(conn, max_age_days=90):
    """Alert ako su tajne (HEP/SMA/HA) starije od max_age_days.

    _secrets_updated postavlja postavke.py pri spremanju tajni; ako fali,
    init_db ga postavi na datum migracije. Podsjetnik max jednom po mjesecu.
    """
    updated = _get_cfg(conn, '_secrets_updated')
    if not updated:
        return False
    today = datetime.now().strftime('%Y-%m-%d')
    try:
        dana = (datetime.fromisoformat(today) - datetime.fromisoformat(updated[:10])).days
    except ValueError:
        return False
    if dana < max_age_days:
        log.info('check_token_age: OK (tajne stare %d dana)', dana)
        return False
    ovaj_mj = today[:7]
    if _get_cfg(conn, '_token_age_warn_month') == ovaj_mj:
        return False
    log.warning('check_token_age: tajne stare %d dana (od %s)', dana, updated)
    if not ha_notify('Energetski Monitor — sigurnost',
                     f'HEP/SMA/HA tajne su stare {dana} dana (zadnja promjena {updated[:10]}). '
                     'Razmisli o rotaciji tokena/lozinki u Postavkama.'):
        return False
    _set_cfg(conn, '_token_age_warn_month', ovaj_mj)
    return True


def check_inverter_balance(conn, threshold_pp=None, min_day_kwh=None):
    """Alarm ako se udio jednog invertera u dnevnoj proizvodnji znacajno
    promijeni u odnosu na povijesni baseline (pokvareni string/MPPT/panel).

    Kapacitet-agnosticno: gleda udio INV1 = inv1/(inv1+inv2). Taj udio je
    stabilan (~konstantan) dok su oba invertera zdrava; nagli pomak znaci da
    jedan podbacuje. Baseline = medijan udjela zadnjih 60 dobrih dana.
    Okida na zadnji KOMPLETAN dan (ne danas), samo iznad min_day_kwh (da nema
    laznih alarma po oblacnim danima), max jednom po datumu.
    Dan se bira po UKUPNOJ proizvodnji (NULL = 0): inverter koji nije dao nista
    ima udio 0% => maksimalno odstupanje (mrtav inverter mora okinuti alarm).
    Baseline se i dalje racuna samo iz zdravih dana (oba > 0).
    """
    if threshold_pp is None:
        try: threshold_pp = float(os.environ.get('INV_BALANCE_THRESHOLD_PP', '8'))
        except ValueError: threshold_pp = 8.0
    if min_day_kwh is None:
        try: min_day_kwh = float(os.environ.get('INV_BALANCE_MIN_KWH', '20'))
        except ValueError: min_day_kwh = 20.0

    # sma_dnevna.datum je LOKALNI dan -> 'danas' takodjer lokalno
    row = conn.execute(
        "SELECT datum, COALESCE(pv_kwh_inv1,0) i1, COALESCE(pv_kwh_inv2,0) i2 FROM sma_dnevna "
        "WHERE datum < date('now','localtime') "
        "AND COALESCE(pv_kwh_inv1,0) + COALESCE(pv_kwh_inv2,0) >= ? "
        "AND COALESCE(pv_kwh_inv1,0) + COALESCE(pv_kwh_inv2,0) > 0 "
        "ORDER BY datum DESC LIMIT 1", (min_day_kwh,)).fetchone()
    if not row:
        log.info('check_inverter_balance: nema dana s dovoljno proizvodnje (>= %.1f kWh)', min_day_kwh)
        return False
    datum, i1, i2 = row['datum'], row['i1'], row['i2']
    tot = i1 + i2
    share1 = i1 / tot * 100

    base = conn.execute(
        "SELECT pv_kwh_inv1*100.0/(pv_kwh_inv1+pv_kwh_inv2) s FROM sma_dnevna "
        "WHERE datum < ? AND pv_kwh_inv1 > 0 AND pv_kwh_inv2 > 0 "
        "AND (pv_kwh_inv1+pv_kwh_inv2) >= ? ORDER BY datum DESC LIMIT 60",
        (datum, min_day_kwh)).fetchall()
    if len(base) < 10:
        log.info('check_inverter_balance: premalo referentnih dana (%d)', len(base))
        return False
    shares = sorted(x['s'] for x in base)
    med = shares[len(shares) // 2]
    dev = share1 - med
    log.info('check_inverter_balance: %s INV1 %.1f%% (baseline %.1f%%, dev %+.1f pp)',
             datum, share1, med, dev)
    if abs(dev) < threshold_pp:
        return False
    if _get_cfg(conn, '_inv_balance_last_date') == datum:
        log.info('check_inverter_balance: %s vec javljen', datum)
        return False
    losi = 'INV1' if dev < 0 else 'INV2'
    stanje = 'NE PROIZVODI' if min(i1, i2) <= 0 else 'podbacuje'
    poruka = (f'Inverter disbalans {datum}: INV1={i1:.1f} kWh ({share1:.0f}%), '
              f'INV2={i2:.1f} kWh ({100-share1:.0f}%). {losi} {stanje} '
              f'(INV1 normalno ~{med:.0f}%). Provjeri string/MPPT/panele/zasjenjenje.')
    log.warning(poruka)
    if not ha_notify('Energetski Monitor — inverter', poruka):
        return False   # bez markera — pokusaj opet pri sljedecem pokretanju
    _set_cfg(conn, '_inv_balance_last_date', datum)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--backfill-daily', action='store_true')
    ap.add_argument('--normalize-ts', action='store_true')
    ap.add_argument('--prune-live', action='store_true')
    ap.add_argument('--keep-days', type=int, default=30)
    ap.add_argument('--backfill-range', nargs=2, metavar=('OD', 'DO'))
    ap.add_argument('--check-freshness', action='store_true')
    ap.add_argument('--check-anomaly', action='store_true')
    ap.add_argument('--check-inverter', action='store_true')
    ap.add_argument('--bill-reminder', action='store_true')
    ap.add_argument('--restore-drill', action='store_true')
    ap.add_argument('--check-offsite', action='store_true')
    ap.add_argument('--check-token-age', action='store_true')
    a = ap.parse_args(argv)

    # (ime koraka, ukljucen?, poziv) — redoslijed je bitan (normalize prije backfilla)
    koraci = [
        ('backfill-range',  bool(a.backfill_range),
         lambda c: backfill_range(c, a.backfill_range[0], a.backfill_range[1])),
        ('normalize-ts',    a.all or a.normalize_ts,    lambda c: normalize_ts(c)),
        ('backfill-daily',  a.all or a.backfill_daily,  lambda c: backfill_daily(c)),
        ('prune-live',      a.all or a.prune_live,      lambda c: prune_live(c, a.keep_days)),
        ('check-freshness', a.all or a.check_freshness, lambda c: check_freshness(c)),
        ('check-anomaly',   a.all or a.check_anomaly,   lambda c: check_anomaly(c)),
        ('check-inverter',  a.all or a.check_inverter,  lambda c: check_inverter_balance(c)),
        ('bill-reminder',   a.all or a.bill_reminder,   lambda c: bill_reminder(c)),
        ('restore-drill',   a.all or a.restore_drill,   lambda c: restore_drill(c)),
        ('check-offsite',   a.all or a.check_offsite,   lambda c: check_offsite(c)),
        ('check-token-age', a.all or a.check_token_age, lambda c: check_token_age(c)),
    ]
    aktivni = [(ime, fn) for ime, ukljucen, fn in koraci if ukljucen]
    if not aktivni:
        ap.print_help()
        return 0

    conn = _conn()
    pali = []
    try:
        for ime, fn in aktivni:
            # svaki korak izoliran: greska u jednom (npr. "database is locked")
            # ne smije preskociti ostale
            try:
                fn(conn)
            except Exception as e:
                pali.append(ime)
                # sqlite poruka je bezopasna i korisna; za ostale samo tip
                # (tekst mrezne iznimke moze sadrzavati URL)
                detalj = f': {e}' if isinstance(e, (sqlite3.Error, ValueError, KeyError)) else ''
                log.error('korak %s NEUSPJESAN: %s%s', ime, type(e).__name__, detalj)
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
    finally:
        conn.close()
    if pali:
        log.error('maintenance: neuspjeli koraci: %s', ', '.join(pali))
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
