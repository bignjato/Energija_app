"""/api/postavke* + /api/postavke/korisnici + backup endpoints (admin)."""

import io
import os
import shutil
from datetime import datetime

from flask import Blueprint, jsonify, request, send_file

from ..core import admin_required, hash_password
from ..db import DB_PATH, get_db, get_config, set_config

bp = Blueprint('postavke', __name__, url_prefix='/api/postavke')

# Ključevi pohranjeni isključivo u config tablici (ne u .env)
DB_ONLY_KEYS = ['VT_UDIO_PERC']


@bp.route('', methods=['GET', 'POST'])
@admin_required
def api_postavke():
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), '.env')

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
    for key in all_keys:
        if key in data and data[key] != '':
            existing[key] = data[key]

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
    with open(env_path, 'w') as f:
        f.writelines(lines)
    try:
        os.chmod(env_path, 0o600)
    except Exception:
        pass

    for k, v in existing.items():
        os.environ[k] = v

    return jsonify({
        'ok': True,
        'warning': 'Promjene credentialsa vrijede tek nakon restarta sync kontejnera.',
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
            username = d.get('username', '').strip()
            password = d.get('password', '')
            uloga    = d.get('uloga', 'viewer')
            if not username or not password:
                return jsonify({'ok': False, 'error': 'Korisnik i lozinka su obavezni'})
            conn.execute(
                'INSERT OR REPLACE INTO korisnici (username, password_hash, uloga) VALUES (?, ?, ?)',
                (username, hash_password(password), uloga),
            )
            conn.commit()
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


@bp.route('/backup')
@admin_required
def api_backup():
    """Download backup baze (BytesIO, bez tempfile leaka)."""
    try:
        with open(DB_PATH, 'rb') as f:
            data = io.BytesIO(f.read())
        data.seek(0)
        datum = datetime.now().strftime('%Y%m%d_%H%M')
        return send_file(data, as_attachment=True,
                         download_name=f'hep_energy_backup_{datum}.db',
                         mimetype='application/octet-stream')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@bp.route('/backup/auto', methods=['POST'])
@admin_required
def api_backup_auto():
    backup_dir = os.path.join(os.path.dirname(DB_PATH), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    datum = datetime.now().strftime('%Y%m%d_%H%M')
    backup_path = os.path.join(backup_dir, f'hep_energy_{datum}.db')
    try:
        shutil.copy2(DB_PATH, backup_path)
        backups = sorted(f for f in os.listdir(backup_dir) if f.endswith('.db'))
        for old in backups[:-7]:
            os.remove(os.path.join(backup_dir, old))
        return jsonify({'ok': True, 'path': backup_path, 'n_backups': len(backups)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})
