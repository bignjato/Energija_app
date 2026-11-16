"""/api/postavke* + /api/postavke/korisnici + backup endpoints (admin)."""

import logging
import os
import sqlite3
import tempfile
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file, session

from ..core import admin_required, hash_password, session_fingerprint
from ..db import DB_PATH, get_db, get_config, set_config

log = logging.getLogger(__name__)

bp = Blueprint('postavke', __name__, url_prefix='/api/postavke')

DOZVOLJENE_ULOGE = ('admin', 'viewer')

ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env')

# Ključevi pohranjeni isključivo u config tablici (ne u .env)
DB_ONLY_KEYS = [
    'VT_UDIO_PERC',
    'PV_NOMINAL_KW',         # npr. 30 (kWp)
    'PV_COMMISSION_DATE',    # YYYY-MM-DD
    'PV_CO2_FACTOR_G_KWH',   # default 640 (HR mix)
    'PV_FEED_TARIFF_EUR',    # cijena otkupa za reimbursement; default = HEP_TARIFA.otkup
    'PV_INVESTMENT_EUR',     # cijena PV sustava (€) — za ROI / povrat investicije
    'PV_LAT',                # latitude (npr. 45.732 za Odru/Lukavec)
    'PV_LON',                # longitude (npr. 16.087)
    'HA_ENT_POWER_LIMIT',    # entity_id za inverter power limit (W)
]


@bp.route('', methods=['GET', 'POST'])
@admin_required
def api_postavke():
    env_path = ENV_PATH

    safe_keys = ['HEP_USERNAME', 'HEP_SIFRA', 'SMA_USERNAME', 'SMA_PLANT_ID',
                 'SMA_INV1_ID', 'SMA_INV2_ID', 'HA_URL', 'TARIFA_VT', 'TARIFA_NT',
                 'TARIFA_PROD', 'TARIFA_PDV', 'TARIFA_VT_OD', 'TARIFA_VT_DO']
    all_keys = safe_keys + ['HEP_PASSWORD', 'SMA_PASSWORD', 'HA_TOKEN',
                            'DASHBOARD_PASSWORD', 'SMA_CLIENT_ID', 'DB_PATH']

    if request.method == 'GET':
        cfg = {k: os.environ.get(k, '') for k in safe_keys}
        cfg['HEP_PASSWORD_SET']       = bool(os.environ.get('HEP_PASSWORD'))
        cfg['SMA_PASSWORD_SET']       = bool(os.environ.get('SMA_PASSWORD'))
        cfg['HA_TOKEN_SET']           = bool(os.environ.get('HA_TOKEN'))
        cfg['DASHBOARD_PASSWORD_SET'] = bool(os.environ.get('DASHBOARD_PASSWORD'))
        # DB-only ključevi
        cfg['VT_UDIO_PERC'] = get_config('VT_UDIO_PERC', '45')
        cfg['PV_INVESTMENT_EUR'] = get_config('PV_INVESTMENT_EUR', '')
        cfg['PV_COMMISSION_DATE'] = get_config('PV_COMMISSION_DATE', '')
        return jsonify(cfg)

    data = request.get_json() or {}

    # DB-only ključevi — direktno u config tablicu
    for k in DB_ONLY_KEYS:
        if k in data and data[k] != '':
            set_config(k, str(data[k]))

    existing = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    existing[k.strip()] = v.strip()

    # SECRET_KEY se NIKAD ne mijenja preko UI-a (rotacija invalidira sve sesije)
    SECRET_KEYS = {'HEP_PASSWORD', 'SMA_PASSWORD', 'HA_TOKEN', 'DASHBOARD_PASSWORD',
                   'SMA_CLIENT_ID'}
    secret_changed = False
    env_changed = False
    tarifa_poslana = False
    for key in all_keys:
        if key in data and data[key] != '':
            if key.startswith('TARIFA_'):
                # ne cita se iz .env-a (vidi poruku dolje) — nije "env promjena"
                tarifa_poslana = True
            elif str(existing.get(key, os.environ.get(key, ''))) != str(data[key]):
                # UI uvijek salje i nepromijenjena (prefillana) polja — upozorenje
                # samo kad se vrijednost stvarno razlikuje
                env_changed = True
                if key in SECRET_KEYS:
                    secret_changed = True
            existing[key] = str(data[key])   # os.environ prima samo str
    # Zabiljezi datum promjene tajni (za check_token_age podsjetnik o rotaciji)
    if secret_changed:
        set_config('_secrets_updated', datetime.now().strftime('%Y-%m-%d'))

    lines = ['# HEP Energy Monitor konfiguracija\n']
    sections = {
        'HEP':    ['HEP_USERNAME', 'HEP_PASSWORD', 'HEP_SIFRA'],
        'SMA':    ['SMA_USERNAME', 'SMA_PASSWORD', 'SMA_CLIENT_ID',
                   'SMA_PLANT_ID', 'SMA_INV1_ID', 'SMA_INV2_ID'],
        'HA':     ['HA_URL', 'HA_TOKEN'],
        'APP':    ['DASHBOARD_PASSWORD', 'DB_PATH', 'SECRET_KEY'],
        'TARIFA': ['TARIFA_VT', 'TARIFA_NT', 'TARIFA_PROD',
                   'TARIFA_PDV', 'TARIFA_VT_OD', 'TARIFA_VT_DO'],
    }
    for section, keys in sections.items():
        lines.append(f'\n# {section}\n')
        for k in keys:
            v = existing.get(k, '')
            lines.append(f'{k}={v}\n')
    # Sacuvaj i kljuceve koje UI ne poznaje (SMA_USE_API, OFFSITE_*, BRAND_*...)
    poznati = {k for keys in sections.values() for k in keys}
    ostali = [k for k in existing if k not in poznati]
    if ostali:
        lines.append('\n# OSTALO (izvan UI postavki)\n')
        for k in sorted(ostali):
            lines.append(f'{k}={existing[k]}\n')
    with open(env_path, 'w') as f:
        f.writelines(lines)
    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass

    for k, v in existing.items():
        os.environ[k] = v

    # .env unutar web kontejnera ne cita nitko (sync dobiva okolinu iz hostovog
    # .env preko docker-compose env_file) i nestaje kad se kontejner rekreira.
    poruke = []
    if env_changed:
        poruke.append('Ove vrijednosti (HEP/SMA/HA pristupni podaci, DASHBOARD_PASSWORD) '
                      'zapisane su samo privremeno unutar web kontejnera i NE '
                      'primjenjuju se na sync. Vrijede tek kad ih promijeniš i u .env '
                      'datoteci na serveru i ponovno kreiraš kontejnere '
                      '(docker compose up -d --force-recreate).')
    # TARIFA_*: get_config cita config tablicu (init_db je uvijek popuni) prije
    # okoline, pa ni .env na serveru tu nista ne mijenja; cijene idu s racuna.
    if tarifa_poslana:
        poruke.append('TARIFA_* vrijednosti se odavde NE primjenjuju (ni preko .env '
                      'datoteke na serveru): jedinične cijene uzimaju se s uvezenih '
                      'računa, a sati VT/NT iz config tablice u bazi.')
    return jsonify({
        'ok': True,
        'warning': ' '.join(poruke),
        'env_restart_required': env_changed,
        'tarifa_ignored': tarifa_poslana,
    })


@bp.route('/mjerno-mjesto')
def api_mjerno_mjesto():
    """Info o mjernom mjestu(ima) iz HEP ODS."""
    conn = get_db()
    try:
        rows = conn.execute('''
            SELECT m.id, m.naziv, m.adresa, m.oib, m.tip, m.napon, m.created_at,
                   (SELECT MIN(ts) FROM ocitanja_15min WHERE mjerno_mjesto=m.id) as prvi_zapis,
                   (SELECT MAX(ts) FROM ocitanja_15min WHERE mjerno_mjesto=m.id) as zadnji_zapis,
                   (SELECT COUNT(*) FROM ocitanja_15min WHERE mjerno_mjesto=m.id) as n_15min,
                   (SELECT ROUND(SUM(kwh_plus),2) FROM ocitanja_dnevna WHERE mjerno_mjesto=m.id) as uk_potrosnja,
                   (SELECT ROUND(SUM(kwh_minus),2) FROM ocitanja_dnevna WHERE mjerno_mjesto=m.id) as uk_predaja
            FROM mjerna_mjesta m
        ''').fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@bp.route('/status')
def api_postavke_status():
    conn = get_db()
    try:
        tables = {}
        for tbl in ['ocitanja_15min', 'ocitanja_satna', 'ocitanja_dnevna',
                    'sma_15min', 'sma_live', 'sma_dnevna', 'racuni']:
            try:
                tables[tbl] = conn.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]
            except Exception:
                tables[tbl] = None

        hep_last = conn.execute('SELECT MAX(ts) FROM ocitanja_satna').fetchone()[0]

        sma_last = None
        try:
            sma_last = conn.execute('SELECT MAX(ts) FROM sma_live').fetchone()[0]
        except Exception:
            pass

        hep_range = conn.execute(
            'SELECT MIN(datum), MAX(datum) FROM ocitanja_dnevna'
        ).fetchone()

        sma_range = (None, None)
        try:
            sma_range = conn.execute(
                'SELECT MIN(ts), MAX(ts) FROM sma_15min'
            ).fetchone()
        except Exception:
            pass

        db_size = 0
        try:
            db_size = os.path.getsize(DB_PATH)
        except Exception:
            pass

        return jsonify({
            'version':       '1.3.0',
            'tables':        tables,
            'hep_last_sync': hep_last,
            'sma_last_sync': sma_last,
            'hep_range':     {'od': hep_range[0], 'do': hep_range[1]},
            'sma_range':     {'od': sma_range[0], 'do': sma_range[1]},
            'db_size_mb':    round(db_size / 1024 / 1024, 2),
            'ts':            datetime.now().isoformat(),
        })
    finally:
        conn.close()


@bp.route('/korisnici', methods=['GET', 'POST', 'DELETE'])
@admin_required
def api_korisnici():
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = conn.execute(
                'SELECT id, username, uloga, aktivan, stvoren, zadnja_prijava FROM korisnici'
            ).fetchall()
            return jsonify([dict(r) for r in rows])

        if request.method == 'POST':
            d = request.get_json() or {}
            username = d.get('username', '')
            password = d.get('password', '')
            uloga    = d.get('uloga', 'viewer')
            if not isinstance(username, str) or not isinstance(password, str):
                return jsonify({'ok': False, 'error': 'Korisnik i lozinka su obavezni'})
            username = username.strip()
            if not username or not password:
                return jsonify({'ok': False, 'error': 'Korisnik i lozinka su obavezni'})
            if uloga not in DOZVOLJENE_ULOGE:
                return jsonify({'ok': False, 'error': 'Nepoznata uloga (dozvoljeno: admin, viewer)'})
            pw_hash = hash_password(password)
            # BEGIN IMMEDIATE: provjera zadnjeg admina i upis u istoj transakciji
            conn.execute('BEGIN IMMEDIATE')
            postojeci = conn.execute(
                'SELECT uloga, aktivan FROM korisnici WHERE username=?', (username,)
            ).fetchone()
            if postojeci:
                if postojeci['uloga'] == 'admin' and postojeci['aktivan'] and uloga != 'admin':
                    admins = conn.execute(
                        "SELECT COUNT(*) FROM korisnici WHERE uloga='admin' AND aktivan=1"
                    ).fetchone()[0]
                    if admins <= 1:
                        conn.rollback()
                        return jsonify({'ok': False, 'error': 'Ne možete degradirati zadnjeg admina!'})
                # UPDATE (ne INSERT OR REPLACE) — id, aktivan, stvoren, zadnja_prijava ostaju
                conn.execute(
                    'UPDATE korisnici SET password_hash=?, uloga=? WHERE username=?',
                    (pw_hash, uloga, username),
                )
            else:
                conn.execute(
                    'INSERT INTO korisnici (username, password_hash, uloga) VALUES (?, ?, ?)',
                    (username, pw_hash, uloga),
                )
            conn.commit()
            # Promjena lozinke invalidira sve sesije tog korisnika; vlastitu
            # (ovu) osvjezi da se admin ne odjavi sam sebi usred rada.
            if session.get('auth_src') == 'db' and session.get('username') == username:
                session['pwfp'] = session_fingerprint(pw_hash)
            return jsonify({'ok': True})

        # DELETE
        username = request.args.get('username', '')
        admins = conn.execute(
            "SELECT COUNT(*) FROM korisnici WHERE uloga='admin' AND aktivan=1"
        ).fetchone()[0]
        uloga_k = conn.execute(
            "SELECT uloga FROM korisnici WHERE username=?", (username,)
        ).fetchone()
        if uloga_k and uloga_k[0] == 'admin' and admins <= 1:
            return jsonify({'ok': False, 'error': 'Ne možete obrisati zadnjeg admina!'})
        conn.execute('DELETE FROM korisnici WHERE username=?', (username,))
        conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


def _online_backup(dest_path: str):
    """Konzistentna kopija baze preko sqlite online backup API-ja.

    Sirovo kopiranje datoteke u WAL modu (dok sync pise) moze dati
    potrganu ili zastarjelu kopiju — backup API cita kroz SQLite.
    """
    src = get_db()
    try:
        dst = sqlite3.connect(dest_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


@bp.route('/backup')
@admin_required
def api_backup():
    """Download backup baze (online backup u temp datoteku, pa stream)."""
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(prefix='hep_backup_', suffix='.db')
        os.close(fd)
        _online_backup(tmp_path)
        f = open(tmp_path, 'rb')
        size = os.fstat(f.fileno()).st_size
        datum = datetime.now().strftime('%Y%m%d_%H%M')
        resp = send_file(f, as_attachment=True,
                         download_name=f'hep_energy_backup_{datum}.db',
                         mimetype='application/octet-stream')
        resp.headers['Content-Length'] = str(size)
        return resp
    except Exception:
        log.exception('Backup download nije uspio')
        return jsonify({'error': 'Backup nije uspio — vidi server log'}), 500
    finally:
        # Otvoreni handle ostaje citljiv i nakon unlinka (POSIX), pa temp
        # datoteka nestaje i ako klijent prekine download.
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


@bp.route('/backup/auto', methods=['POST'])
@admin_required
def api_backup_auto():
    backup_dir = os.path.join(os.path.dirname(DB_PATH), 'backups')
    datum = datetime.now().strftime('%Y%m%d_%H%M')
    backup_path = os.path.join(backup_dir, f'hep_energy_{datum}.db')
    tmp_path = backup_path + '.tmp'
    try:
        os.makedirs(backup_dir, exist_ok=True)
        # prvo u .tmp pa rename — prekinuti backup ne ostavlja polovicnu .db
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        _online_backup(tmp_path)
        os.replace(tmp_path, backup_path)
        backups = sorted(f for f in os.listdir(backup_dir) if f.endswith('.db'))
        for old in backups[:-7]:
            os.remove(os.path.join(backup_dir, old))
        return jsonify({'ok': True, 'path': backup_path, 'n_backups': len(backups)})
    except Exception:
        log.exception('Auto backup nije uspio')
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return jsonify({'ok': False, 'error': 'Backup nije uspio — vidi server log'})
