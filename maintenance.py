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
"""
import os, sqlite3, logging, argparse, sys
from datetime import datetime, timedelta, timezone

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('maintenance')

DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')


def _conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


# ---------- 1. Backfill sma_dnevna flow-metrika iz sma_live ----------
def backfill_daily(conn):
    """Dnevne feed/grid/cons kWh + autarkija iz sma_live (W uzorci).

    Integracija: trapezno po danu (W × dt). Samo dani gdje sma_dnevna jos
    nema flow podatke (feed_in_kwh IS NULL). PV ostaje iz sma_15min importa.
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
        po_danu.setdefault(dt.date().isoformat(), []).append((dt, r))

    # koje dane treba (sma_dnevna.feed_in_kwh IS NULL ili red ne postoji)
    treba = set()
    for d in po_danu:
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
    conn.execute("DELETE FROM sma_live WHERE ts < datetime('now', ?)", (f'-{keep_days} days',))
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
    # agregiraj te dane u sma_dnevna (PV)
    conn.execute('''
        INSERT OR REPLACE INTO sma_dnevna (datum, pv_generation_kwh, pv_kwh_inv1, pv_kwh_inv2)
        SELECT date(ts), ROUND(SUM(pv_w_total)*0.25/1000.0,3),
               ROUND(SUM(pv_w_inv1)*0.25/1000.0,3), ROUND(SUM(pv_w_inv2)*0.25/1000.0,3)
        FROM sma_15min WHERE pv_w_total > 0 AND date(ts) BETWEEN ? AND ?
        GROUP BY date(ts)
    ''', (od, do))
    conn.commit()
    log.info('backfill_range %s..%s: %d 15-min zapisa', od, do, total)
    return total


# ---------- HA notifikacije ----------
def ha_notify(title, message):
    """Posalji HA notifikaciju. Vraca True ako je poslana."""
    import requests
    from netutil import ha_verify
    ha_url = os.environ.get('HA_URL', '').strip().rstrip('/')
    token  = os.environ.get('HA_TOKEN', '').strip()
    notify = os.environ.get('HA_NOTIFY_SERVICE', 'notify/persistent_notification')
    if not ha_url or not token:
        log.error('ha_notify: HA_URL/HA_TOKEN nisu postavljeni — preskacem notifikaciju')
        return False
    try:
        r = requests.post(f'{ha_url}/api/services/{notify}',
                          headers={'Authorization': f'Bearer {token}'},
                          json={'title': title, 'message': message},
                          timeout=15, verify=ha_verify())
        log.info('ha_notify: %s -> %s', notify, r.status_code)
        return r.ok
    except Exception as e:
        log.error('ha_notify greska: %s', e)
        return False


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
def check_freshness(conn, max_hep_h=48, max_sma_h=12):
    """Posalji HA notifikaciju ako HEP/SMA podaci kasne."""
    problemi = []
    hep = conn.execute('SELECT MAX(datum) d FROM ocitanja_dnevna').fetchone()['d']
    if hep:
        zaost = (datetime.now().date() - datetime.fromisoformat(hep).date()).days
        if zaost >= 2:
            problemi.append(f'HEP podaci kasne {zaost} dana (zadnji {hep})')
    sma = conn.execute("SELECT MAX(ts) t FROM sma_live").fetchone()['t']
    if sma:
        try:
            dt = datetime.fromisoformat(sma.replace('Z', '+00:00'))
            h = (datetime.now(timezone.utc) - dt).total_seconds() / 3600
            if h >= max_sma_h:
                problemi.append(f'SMA live kasni {h:.0f} h (zadnji {sma})')
        except Exception:
            pass
    errs = conn.execute("SELECT COUNT(*) c FROM sync_log WHERE status='ERROR' AND ts >= datetime('now','-1 day')").fetchone()['c']
    if errs >= 20:
        problemi.append(f'{errs} sync gresaka u zadnja 24 h')

    if not problemi:
        log.info('check_freshness: OK')
        return False
    poruka = 'Energetski Monitor — problemi:\n• ' + '\n• '.join(problemi)
    log.warning(poruka.replace('\n', ' | '))
    ha_notify('Energetski Monitor — alert', poruka)
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
    Notifikacija max jednom po datumu.
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

    zadnji = conn.execute(
        'SELECT datum, ROUND(SUM(kwh_plus),2) kp FROM ocitanja_dnevna '
        'GROUP BY datum ORDER BY datum DESC LIMIT 1').fetchone()
    if not zadnji or not zadnji['kp']:
        log.info('check_anomaly: nema dnevnih podataka')
        return False
    datum, kp = zadnji['datum'], zadnji['kp']

    if _get_cfg(conn, '_anomaly_last_date') == datum:
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
    _set_cfg(conn, '_anomaly_last_date', datum)

    if not wd or (wd['n'] or 0) < min_samples or (wd['avg_kp'] or 0) < min_avg_kwh:
        log.info('check_anomaly: premalo referentnih dana (%s)', wd['n'] if wd else 0)
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
        return False
    ha_notify('Energetski Monitor — anomalija potrosnje',
              f'{datum}: potrosnja {kp:.1f} kWh je {odst_wd:.0f}% iznad prosjeka za taj dan u '
              f'tjednu ({avg_wd:.1f} kWh) i {odst_roll:.0f}% iznad prosjeka zadnjih 14 dana ({avg_roll:.1f} kWh).')
    return True


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
    period = f'{prosli.month:02d}/{prosli.year}'   # racuni.period format MM/YYYY

    ovaj_mj = now.strftime('%Y-%m')
    if _get_cfg(conn, '_bill_reminder_last') == ovaj_mj:
        return False
    try:
        ima = conn.execute('SELECT 1 FROM racuni WHERE period=?', (period,)).fetchone()
    except sqlite3.Error:
        return False   # tablica jos ne postoji (svjezа instalacija)
    if ima:
        return False
    _set_cfg(conn, '_bill_reminder_last', ovaj_mj)
    ha_notify('Energetski Monitor — racun',
              f'HEP racun za {period} jos nije unesen. Uploadaj PDF u Racuni tab — '
              'projekcija duga je tocnija sa svakim racunom.')
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

    _set_cfg(conn, '_restore_drill_last', today)
    if problemi:
        poruka = f'Restore drill NEUSPJESAN ({os.path.basename(latest)}):\n• ' + '\n• '.join(problemi)
        log.error(poruka.replace('\n', ' | '))
        ha_notify('Energetski Monitor — backup PROBLEM', poruka)
        return False
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
        _set_cfg(conn, '_offsite_warn_last', today)
        log.warning('check_offsite: off-site iskljucen — backupi su samo na lokalnom volumenu')
        ha_notify('Energetski Monitor — backup',
                  'Off-site backup je iskljucen. Backupi baze zive samo na istom volumenu '
                  'kao i baza — gubitak volumena znaci gubitak svega. Postavi OFFSITE_BACKUP_MODE.')
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
    _set_cfg(conn, '_token_age_warn_month', ovaj_mj)
    log.warning('check_token_age: tajne stare %d dana (od %s)', dana, updated)
    ha_notify('Energetski Monitor — sigurnost',
              f'HEP/SMA/HA tajne su stare {dana} dana (zadnja promjena {updated[:10]}). '
              'Razmisli o rotaciji tokena/lozinki u Postavkama.')
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--backfill-daily', action='store_true')
    ap.add_argument('--normalize-ts', action='store_true')
    ap.add_argument('--prune-live', action='store_true')
    ap.add_argument('--keep-days', type=int, default=30)
    ap.add_argument('--backfill-range', nargs=2, metavar=('OD', 'DO'))
    ap.add_argument('--check-freshness', action='store_true')
    ap.add_argument('--check-anomaly', action='store_true')
    ap.add_argument('--bill-reminder', action='store_true')
    ap.add_argument('--restore-drill', action='store_true')
    ap.add_argument('--check-offsite', action='store_true')
    ap.add_argument('--check-token-age', action='store_true')
    a = ap.parse_args()

    conn = _conn()
    try:
        if a.backfill_range:
            backfill_range(conn, a.backfill_range[0], a.backfill_range[1])
        if a.all or a.normalize_ts:
            normalize_ts(conn)
        if a.all or a.backfill_daily:
            backfill_daily(conn)
        if a.all or a.prune_live:
            prune_live(conn, a.keep_days)
        if a.all or a.check_freshness:
            check_freshness(conn)
        if a.all or a.check_anomaly:
            check_anomaly(conn)
        if a.all or a.bill_reminder:
            bill_reminder(conn)
        if a.all or a.restore_drill:
            restore_drill(conn)
        if a.all or a.check_offsite:
            check_offsite(conn)
        if a.all or a.check_token_age:
            check_token_age(conn)
        if not any([a.all, a.backfill_daily, a.normalize_ts, a.prune_live,
                    a.backfill_range, a.check_freshness, a.check_anomaly,
                    a.bill_reminder, a.restore_drill, a.check_offsite,
                    a.check_token_age]):
            ap.print_help()
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
