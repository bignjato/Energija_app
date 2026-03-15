#!/usr/bin/env python3
"""
Flask web server za HEP + SMA energy dashboard
InfoBot — Boris Ignjatović
"""

from flask import Flask, jsonify, request, session, redirect
import sqlite3, os, hashlib, secrets
from datetime import datetime, timedelta

app = Flask(__name__)
DB_PATH    = os.environ.get('DB_PATH', '/data/hep_energy.db')
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'dashboard_template.html')
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# ===== BAZA — INICIJALIZACIJA =====

def init_db():
    """Inicijaliziraj bazu — kreiraj tablice i defaultne vrijednosti"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Config tablica
    conn.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated TEXT DEFAULT (datetime('now'))
        )
    ''')

    # Korisnici tablica
    conn.execute('''
        CREATE TABLE IF NOT EXISTS korisnici (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            uloga TEXT DEFAULT 'viewer',
            aktivan INTEGER DEFAULT 1,
            stvoren TEXT DEFAULT (datetime('now')),
            zadnja_prijava TEXT
        )
    ''')

    # Kreiraj defaultnog admin korisnika ako nema nijednog
    count = conn.execute('SELECT COUNT(*) FROM korisnici').fetchone()[0]
    if count == 0:
        salt = secrets.token_hex(16)
        pw_hash = hashlib.sha256(f'{salt}:admin'.encode()).hexdigest()
        conn.execute('''
            INSERT INTO korisnici (username, password_hash, uloga)
            VALUES (?, ?, 'admin')
        ''', ('admin', f'{salt}:{pw_hash}'))
        app.logger.info('Kreiran defaultni admin/admin korisnik')

    # Migriraj .env u config tablicu (samo jednom)
    migrated = conn.execute(
        "SELECT value FROM config WHERE key='_migrated'"
    ).fetchone()

    if not migrated:
        env_keys = [
            'HEP_USERNAME', 'HEP_PASSWORD', 'HEP_SIFRA',
            'SMA_USERNAME', 'SMA_PASSWORD', 'SMA_CLIENT_ID',
            'SMA_PLANT_ID', 'SMA_INV1_ID', 'SMA_INV2_ID',
            'HA_URL', 'HA_TOKEN',
            'TARIFA_VT', 'TARIFA_NT', 'TARIFA_PROD',
            'TARIFA_PDV', 'TARIFA_VT_OD', 'TARIFA_VT_DO',
        ]
        for key in env_keys:
            val = os.environ.get(key, '')
            if val:
                conn.execute(
                    'INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)',
                    (key, val)
                )
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('_migrated', '1')")
        app.logger.info('Migriran .env u config tablicu')

    # Defaultne tarife ako ne postoje
    defaults = {
        'TARIFA_VT': '0.131205', 'TARIFA_NT': '0.064379',
        'TARIFA_PROD': '0.064379', 'TARIFA_PDV': '13',
        'TARIFA_VT_OD': '7', 'TARIFA_VT_DO': '21',
    }
    for k, v in defaults.items():
        conn.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)', (k, v))

    conn.commit()
    conn.close()


def get_config(key, default=''):
    """Dohvati konfiguraciju iz baze"""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
        conn.close()
        return row[0] if row else os.environ.get(key, default)
    except:
        return os.environ.get(key, default)


def set_config(key, value):
    """Spremi konfiguraciju u bazu"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT OR REPLACE INTO config (key, value, updated) VALUES (?, ?, datetime("now"))',
        (key, value)
    )
    conn.commit()
    conn.close()


# Pokretanje init pri startu
with app.app_context():
    try:
        init_db()
    except Exception as e:
        print(f'Init DB error: {e}')


# ===== HEP TARIFA =====
HEP_TARIFA = {
    'vt_opskrba':  0.131205,
    'nt_opskrba':  0.064379,
    'vt_distrib':  0.044446,
    'nt_distrib':  0.020514,
    'vt_prijenos': 0.021256,
    'nt_prijenos': 0.008175,
    'solidarna':   0.003982,
    'oie':         0.013239,
    'opskrbna_mj': 0.982,
    'mjerna_mj':   1.983,
    'pdv':         0.13,
    'vt_udio':     0.45,
    'otkup':       0.064379,
}

def izracunaj_racun(kwh_plus, kwh_minus, n_dana=30):
    t = HEP_TARIFA
    vt = kwh_plus * t['vt_udio']
    nt = kwh_plus * (1 - t['vt_udio'])
    n_mj = n_dana / 30.0
    opskrba = (vt * t['vt_opskrba'] + nt * t['nt_opskrba'] +
               kwh_plus * (t['solidarna'] + t['oie']) +
               t['opskrbna_mj'] * n_mj -
               kwh_minus * t['otkup'])
    mreza = (vt * (t['vt_distrib'] + t['vt_prijenos']) +
             nt * (t['nt_distrib'] + t['nt_prijenos']) +
             t['mjerna_mj'] * n_mj)
    osnovica = opskrba + mreza
    return round(osnovica * (1 + t['pdv']), 2)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


@app.route('/')
def index():
    with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
        return f.read()


@app.route('/api/data')
def api_data():
    conn = get_db()
    try:
        # HEP satna za zadnjih 7 dana
        satna = conn.execute('''
            SELECT ts, kwh_plus, kwh_minus
            FROM ocitanja_satna
            WHERE ts <= datetime('now') AND kwh_plus > 0
            ORDER BY ts DESC LIMIT 168
        ''').fetchall()

        # HEP dnevna za zadnjih 90 dana
        dnevna = conn.execute('''
            SELECT datum, kwh_plus, kwh_minus
            FROM ocitanja_dnevna
            WHERE datum <= date('now')
            ORDER BY datum DESC LIMIT 90
        ''').fetchall()

        # Provjeri ima li SMA tablica
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_live'"
        ).fetchone() is not None

        sma_live = None
        sma_dnevna = []
        sma_satna = []

        if has_sma:
            # Zadnji SMA live zapis
            sma_live_row = conn.execute('''
                SELECT ts, pv_generation_w, feed_in_w, external_consumption_w,
                       total_consumption_w, direct_consumption_w, autarky_rate, self_consumption_rate
                FROM sma_live ORDER BY ts DESC LIMIT 1
            ''').fetchone()
            if sma_live_row:
                sma_live = dict(sma_live_row)

            # SMA dnevna
            sma_dnevna_rows = conn.execute('''
                SELECT datum, pv_generation_kwh, feed_in_kwh, grid_consumption_kwh,
                       total_consumption_kwh, self_consumption_kwh, autarky_rate
                FROM sma_dnevna
                ORDER BY datum DESC LIMIT 90
            ''').fetchall()
            sma_dnevna = [dict(r) for r in sma_dnevna_rows]

            # SMA satni iz live (grupirano po satu)
            sma_satna_rows = conn.execute('''
                SELECT 
                    strftime('%Y-%m-%dT%H:00:00', ts) as sat,
                    ROUND(AVG(pv_generation_w), 0) as pv_w,
                    ROUND(AVG(feed_in_w), 0) as feed_w,
                    ROUND(AVG(external_consumption_w), 0) as grid_w,
                    ROUND(AVG(total_consumption_w), 0) as total_w
                FROM sma_live
                WHERE ts >= datetime('now', '-7 days') AND ts <= datetime('now')
                GROUP BY strftime('%Y-%m-%dT%H:00:00', ts)
                ORDER BY sat DESC LIMIT 168
            ''').fetchall()
            sma_satna = [dict(r) for r in sma_satna_rows]

        # Tarifa
        tarifa = None
        has_tarife = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tarife'"
        ).fetchone() is not None
        if has_tarife:
            tarifa_row = conn.execute(
                'SELECT * FROM tarife WHERE aktivan=1 ORDER BY id DESC LIMIT 1'
            ).fetchone()
            if tarifa_row:
                tarifa = dict(tarifa_row)

        return jsonify({
            'satna': [dict(r) for r in reversed(satna)],
            'dnevna': [dict(r) for r in reversed(dnevna)],
            'sma_live': sma_live,
            'sma_dnevna': list(reversed(sma_dnevna)),
            'sma_satna': list(reversed(sma_satna)),
            'tarifa': tarifa,
            'ts': datetime.now().isoformat(),
        })
    finally:
        conn.close()


@app.route('/api/tarifa', methods=['GET', 'POST'])
def api_tarifa():
    conn = get_db()
    try:
        if request.method == 'POST':
            data = request.get_json()
            # Deaktiviraj sve stare
            conn.execute('UPDATE tarife SET aktivan=0')
            conn.execute('''
                INSERT INTO tarife (naziv, cijena_kupnja, cijena_prodaja, vt_pocetak, vt_kraj, aktivan)
                VALUES (?, ?, ?, ?, ?, 1)
            ''', (
                data.get('naziv', 'Moja tarifa'),
                float(data.get('cijena_kupnja', 0.12)),
                float(data.get('cijena_prodaja', 0.065)),
                int(data.get('vt_pocetak', 7)),
                int(data.get('vt_kraj', 21)),
            ))
            conn.commit()
            return jsonify({'ok': True})

        rows = conn.execute('SELECT * FROM tarife ORDER BY id DESC').fetchall()
        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route('/api/stats/usporedba')
def api_usporedba():
    """Usporedba HEP vs SMA podataka"""
    conn = get_db()
    try:
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_dnevna'"
        ).fetchone() is not None

        if not has_sma:
            return jsonify({'error': 'Nema SMA podataka'})

        # JOIN HEP i SMA dnevnih podataka
        rows = conn.execute('''
            SELECT 
                h.datum,
                h.kwh_plus as hep_potrosnja,
                h.kwh_minus as hep_predaja,
                s.pv_generation_kwh as sma_proizvodnja,
                s.feed_in_kwh as sma_predaja,
                s.grid_consumption_kwh as sma_mreza,
                s.total_consumption_kwh as sma_potrosnja,
                s.autarky_rate
            FROM ocitanja_dnevna h
            LEFT JOIN sma_dnevna s ON h.datum = s.datum
            WHERE h.datum >= date('now', '-90 days')
            ORDER BY h.datum
        ''').fetchall()

        return jsonify([dict(r) for r in rows])
    finally:
        conn.close()


@app.route('/api/stats/optimalno')
def api_optimalno():
    """Analiza optimalnog vremena potrošnje - po satu dana"""
    conn = get_db()
    try:
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_live'"
        ).fetchone() is not None

        # HEP - prosječna potrošnja po satu
        hep_satno = conn.execute('''
            SELECT 
                CAST(strftime('%H', ts) AS INTEGER) as sat,
                ROUND(AVG(kwh_plus), 4) as prosj_potrosnja,
                ROUND(AVG(kwh_minus), 4) as prosj_predaja,
                COUNT(*) as n
            FROM ocitanja_satna
            WHERE ts <= datetime('now') AND kwh_plus > 0
            GROUP BY CAST(strftime('%H', ts) AS INTEGER)
            ORDER BY sat
        ''').fetchall()

        sma_satno = []
        has_15min = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_15min'"
        ).fetchone() is not None

        if has_15min:
            sma_satno = conn.execute('''
                SELECT 
                    CAST(strftime('%H', ts) AS INTEGER) as sat,
                    ROUND(AVG(pv_w_total) / 1000.0, 3) as prosj_pv_w,
                    ROUND(AVG(pv_w_inv1) / 1000.0, 3) as prosj_inv1_kw,
                    ROUND(AVG(pv_w_inv2) / 1000.0, 3) as prosj_inv2_kw,
                    COUNT(*) as n
                FROM sma_15min
                GROUP BY CAST(strftime('%H', ts) AS INTEGER)
                ORDER BY sat
            ''').fetchall()
        elif has_sma:
            sma_satno = conn.execute('''
                SELECT 
                    CAST(strftime('%H', ts) AS INTEGER) as sat,
                    ROUND(AVG(pv_generation_w) / 1000.0, 3) as prosj_pv_w,
                    0 as prosj_inv1_kw,
                    0 as prosj_inv2_kw,
                    COUNT(*) as n
                FROM sma_live
                WHERE ts <= datetime('now')
                GROUP BY CAST(strftime('%H', ts) AS INTEGER)
                ORDER BY sat
            ''').fetchall()

        return jsonify({
            'hep_satno': [dict(r) for r in hep_satno],
            'sma_satno': [dict(r) for r in sma_satno],
        })
    finally:
        conn.close()


@app.route('/api/sma/live')
def api_sma_live():
    """Trenutni SMA live podaci"""
    conn = get_db()
    try:
        row = conn.execute('''
            SELECT * FROM sma_live ORDER BY ts DESC LIMIT 1
        ''').fetchone()
        if row:
            return jsonify(dict(row))
        return jsonify({'error': 'Nema podataka'})
    finally:
        conn.close()


@app.route('/api/stats/mjesecni')
def api_mjesecni():
    """Mjesečni pregled s procijenjenim računom i usporedbom"""
    conn = get_db()
    try:
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_dnevna'"
        ).fetchone() is not None

        rows = conn.execute('''
            SELECT
                substr(h.datum,1,7) as mjesec,
                ROUND(SUM(h.kwh_plus),2)  as hep_potrosnja,
                ROUND(SUM(h.kwh_minus),2) as hep_predaja,
                COUNT(h.datum) as n_dana
            FROM ocitanja_dnevna h
            WHERE h.datum >= date('now', '-36 months')
            GROUP BY substr(h.datum,1,7)
            ORDER BY mjesec DESC
        ''').fetchall()

        result = []
        for row in rows:
            r = dict(row)
            kp = r['hep_potrosnja'] or 0
            km = r['hep_predaja'] or 0
            n  = r['n_dana'] or 1
            r['procj_racun'] = izracunaj_racun(kp, km, n)
            r['procj_trosak_neto'] = round(
                kp * (HEP_TARIFA['vt_opskrba'] * 0.45 + HEP_TARIFA['nt_opskrba'] * 0.55) -
                km * HEP_TARIFA['otkup'], 2)

            if has_sma:
                sma = conn.execute('''
                    SELECT ROUND(SUM(pv_generation_kwh),2) as pv,
                           ROUND(AVG(autarky_rate)*100,1) as autarkija
                    FROM sma_dnevna WHERE substr(datum,1,7)=?
                ''', (r['mjesec'],)).fetchone()
                r['sma_pv'] = sma['pv'] if sma else None
                r['sma_autarkija'] = sma['autarkija'] if sma else None
            result.append(r)

        # Dohvati ručno unesene račune
        has_racuni = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='racuni'"
        ).fetchone() is not None
        racuni = []
        if has_racuni:
            racuni = [dict(r) for r in conn.execute(
                'SELECT * FROM racuni ORDER BY period DESC'
            ).fetchall()]

        return jsonify({
            'mjeseci': result,
            'tarifa': HEP_TARIFA,
            'racuni': racuni,
        })
    finally:
        conn.close()


@app.route('/api/racuni', methods=['GET', 'POST', 'DELETE'])
def api_racuni():
    """CRUD za ručno unesene HEP račune"""
    conn = get_db()
    try:
        # Kreiraj tablicu ako ne postoji
        conn.execute('''
            CREATE TABLE IF NOT EXISTS racuni (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                period TEXT NOT NULL UNIQUE,
                iznos REAL NOT NULL,
                kwh_plus REAL,
                kwh_minus REAL,
                kwh_vt REAL,
                kwh_nt REAL,
                opskrba REAL,
                mreza REAL,
                pdv REAL,
                napomena TEXT,
                stvoren TEXT DEFAULT (datetime('now'))
            )
        ''')
        conn.commit()

        if request.method == 'GET':
            rows = conn.execute('SELECT * FROM racuni ORDER BY period DESC').fetchall()
            return jsonify([dict(r) for r in rows])

        elif request.method == 'POST':
            d = request.get_json()
            conn.execute('''
                INSERT OR REPLACE INTO racuni
                    (period, iznos, kwh_plus, kwh_minus, kwh_vt, kwh_nt, opskrba, mreza, pdv, napomena)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            ''', (
                d['period'], d['iznos'],
                d.get('kwh_plus'), d.get('kwh_minus'),
                d.get('kwh_vt'), d.get('kwh_nt'),
                d.get('opskrba'), d.get('mreza'), d.get('pdv'),
                d.get('napomena', ''),
            ))
            conn.commit()
            return jsonify({'ok': True})

        elif request.method == 'DELETE':
            period = request.args.get('period')
            if period:
                conn.execute('DELETE FROM racuni WHERE period=?', (period,))
                conn.commit()
            return jsonify({'ok': True})

    finally:
        conn.close()


# ===== LOGIN =====
import hashlib, secrets, functools
from flask import session, redirect

app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
LOGIN_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '')

LOGIN_PAGE = '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>InfoBot Energija — Login</title>
<link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;600;700&family=IBM+Plex+Mono&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080c12;color:#d0dde8;font-family:'IBM Plex Sans',sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh;padding:20px}
.box{background:#0f1623;border:1px solid #1f2d3d;border-radius:14px;padding:40px 36px;width:100%;max-width:400px;box-shadow:0 20px 60px rgba(0,0,0,0.5)}
.logo{display:flex;align-items:center;justify-content:center;margin-bottom:28px}
.logo-text{font-size:32px;font-weight:900;letter-spacing:-1px;line-height:1}
.logo-info{color:#00d4ff}
.logo-bot{color:#e63329}
.subtitle{font-size:11px;color:#526070;text-align:center;margin-top:4px;text-transform:uppercase;letter-spacing:1px}
h2{font-size:16px;font-weight:600;color:#fff;margin-bottom:6px;text-align:center}
p{font-size:13px;color:#526070;margin-bottom:24px;text-align:center}
label{display:block;font-size:11px;color:#526070;text-transform:uppercase;letter-spacing:.6px;margin-bottom:5px}
input{width:100%;padding:11px 14px;background:#080c12;border:1px solid #1f2d3d;
      border-radius:8px;color:#d0dde8;font-size:14px;margin-bottom:16px;font-family:'IBM Plex Mono',monospace;transition:border-color .2s}
input:focus{outline:none;border-color:#22d3ee}
button{width:100%;padding:12px;background:linear-gradient(135deg,#22d3ee,#0891b2);color:#000;border:none;
       border-radius:8px;font-size:14px;font-weight:700;cursor:pointer;letter-spacing:.3px;transition:opacity .2s}
button:hover{opacity:.9}
.err{color:#f87171;font-size:13px;margin-bottom:14px;text-align:center;display:none;
     background:rgba(248,113,113,.1);border:1px solid rgba(248,113,113,.3);border-radius:6px;padding:8px}
.divider{border:none;border-top:1px solid #1f2d3d;margin:20px 0}
.footer{font-size:11px;color:#526070;text-align:center}
</style></head>
<body><div class="box">
  <div class="logo">
    <div>
      <div class="logo-text"><span class="logo-info">INFO</span><span class="logo-bot">BOT</span></div>
      <div class="subtitle">Obrt za informatičke i druge usluge</div>
    </div>
  </div>
  <hr class="divider">
  <h2>Energetski Monitor</h2>
  <p>Boris Ignjatović · Lukavec</p>
  <div class="err" id="err">Pogrešno korisničko ime ili lozinka</div>
  <form method="POST" action="/login">
    <label>Korisničko ime</label>
    <input type="text" name="username" placeholder="korisnik" autocomplete="username" autofocus>
    <label>Lozinka</label>
    <input type="password" name="password" placeholder="••••••••" autocomplete="current-password">
    <button type="submit">→ Prijava</button>
  </form>
  <hr class="divider">
  <div class="footer">© 2024 InfoBot · Starogradska 14, 10412 Lukavec · info@infobot.hr · +385 91 6234446</div>
</div>
<script>if(window.location.search.includes('err'))document.getElementById('err').style.display='block'</script>
</body></html>'''


@app.before_request
def check_login():
    """Provjeri login prije svake rute — uvijek traži prijavu"""
    free = ['/login', '/logout', '/health', '/favicon.ico']
    if request.path in free or request.path.startswith('/static/'):
        return None
    if session.get('logged_in'):
        return None
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Unauthorized'}), 401
    return LOGIN_PAGE


@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        pw = request.form.get('password', '')

        # Provjeri DASHBOARD_PASSWORD (samo lozinka, bez usernamea)
        if LOGIN_PASSWORD and pw == LOGIN_PASSWORD:
            session['logged_in'] = True
            session['username'] = username or 'admin'
            session['uloga'] = 'admin'
            session.permanent = True
            return redirect('/')

        # Provjeri korisničku bazu po usernameu
        try:
            conn = sqlite3.connect(DB_PATH)
            conn.row_factory = sqlite3.Row
            user = conn.execute(
                'SELECT username, password_hash, uloga FROM korisnici WHERE username=? AND aktivan=1',
                (username,)
            ).fetchone()
            conn.close()

            if user:
                stored = user['password_hash']
                valid = False
                if ':' in stored:
                    # Format: salt:sha256hash
                    salt, pw_hash = stored.split(':', 1)
                    test = hashlib.sha256(f'{salt}:{pw}'.encode()).hexdigest()
                    valid = (test == pw_hash)
                else:
                    # Stari format — SHA256 direktno
                    valid = (hashlib.sha256(pw.encode()).hexdigest() == stored)

                if valid:
                    session['logged_in'] = True
                    session['username'] = user['username']
                    session['uloga'] = user['uloga']
                    session.permanent = True
                    conn2 = sqlite3.connect(DB_PATH)
                    conn2.execute(
                        'UPDATE korisnici SET zadnja_prijava=datetime("now") WHERE username=?',
                        (user['username'],)
                    )
                    conn2.commit()
                    conn2.close()
                    return redirect('/')
        except Exception as e:
            app.logger.error(f'Login error: {e}')

        return LOGIN_PAGE.replace('display:none', 'display:block')
    return LOGIN_PAGE


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# Wrap index s login info (za logout gumb)
_orig_index = app.view_functions.get('index')
if _orig_index:
    app.view_functions['index'] = _orig_index


@app.route('/api/data/sve')
def api_data_sve():
    """Prošireni podaci - sve dnevne za povijest"""
    conn = get_db()
    try:
        dnevna = conn.execute('''
            SELECT datum, kwh_plus, kwh_minus
            FROM ocitanja_dnevna
            WHERE datum <= date('now')
            ORDER BY datum DESC
        ''').fetchall()
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_dnevna'"
        ).fetchone() is not None
        sma_dnevna = []
        if has_sma:
            sma_dnevna = conn.execute('''
                SELECT datum, pv_generation_kwh, feed_in_kwh, autarky_rate,
                       pv_kwh_inv1, pv_kwh_inv2
                FROM sma_dnevna ORDER BY datum DESC
            ''').fetchall()
        return jsonify({
            'dnevna': [dict(r) for r in dnevna],
            'sma_dnevna': [dict(r) for r in sma_dnevna],
        })
    finally:
        conn.close()


@app.route('/api/povijest')
def api_povijest():
    """Fleksibilni period za povijest — dnevni i satni podaci"""
    od  = request.args.get('od',  (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'))
    do  = request.args.get('do',  datetime.now().strftime('%Y-%m-%d'))
    res = request.args.get('res', 'day')  # day | hour | week

    conn = get_db()
    try:
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_dnevna'"
        ).fetchone() is not None
        has_15min = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_15min'"
        ).fetchone() is not None

        if res == 'hour':
            # Satni podaci za kraći period
            hep = conn.execute('''
                SELECT ts, kwh_plus, kwh_minus
                FROM ocitanja_satna
                WHERE date(ts) BETWEEN ? AND ? AND ts <= datetime('now')
                ORDER BY ts
            ''', (od, do)).fetchall()

            sma = []
            if has_15min:
                sma = conn.execute('''
                    SELECT strftime('%Y-%m-%dT%H:00:00', ts) as sat,
                           ROUND(SUM(pv_w_total)*0.25/1000.0, 3) as pv_kwh,
                           ROUND(SUM(pv_w_inv1)*0.25/1000.0, 3) as inv1_kwh,
                           ROUND(SUM(pv_w_inv2)*0.25/1000.0, 3) as inv2_kwh
                    FROM sma_15min
                    WHERE date(ts) BETWEEN ? AND ?
                    GROUP BY strftime('%Y-%m-%dT%H:00:00', ts)
                    ORDER BY sat
                ''', (od, do)).fetchall()

            return jsonify({
                'res': 'hour', 'od': od, 'do': do,
                'hep': [dict(r) for r in hep],
                'sma': [dict(r) for r in sma],
            })

        elif res == 'week':
            # Tjedni podaci
            hep = conn.execute('''
                SELECT strftime('%Y-W%W', datum) as tjedan,
                       MIN(datum) as datum_od,
                       ROUND(SUM(kwh_plus), 2) as kwh_plus,
                       ROUND(SUM(kwh_minus), 2) as kwh_minus,
                       COUNT(*) as n_dana
                FROM ocitanja_dnevna
                WHERE datum BETWEEN ? AND ? AND datum <= date('now')
                GROUP BY strftime('%Y-W%W', datum)
                ORDER BY tjedan
            ''', (od, do)).fetchall()

            sma = []
            if has_sma:
                sma = conn.execute('''
                    SELECT strftime('%Y-W%W', datum) as tjedan,
                           ROUND(SUM(pv_generation_kwh), 2) as pv_kwh,
                           ROUND(SUM(pv_kwh_inv1), 2) as inv1_kwh,
                           ROUND(SUM(pv_kwh_inv2), 2) as inv2_kwh
                    FROM sma_dnevna
                    WHERE datum BETWEEN ? AND ?
                    GROUP BY strftime('%Y-W%W', datum)
                    ORDER BY tjedan
                ''', (od, do)).fetchall()

            return jsonify({
                'res': 'week', 'od': od, 'do': do,
                'hep': [dict(r) for r in hep],
                'sma': [dict(r) for r in sma],
            })

        else:
            # Dnevni podaci (default)
            hep = conn.execute('''
                SELECT datum, kwh_plus, kwh_minus
                FROM ocitanja_dnevna
                WHERE datum BETWEEN ? AND ? AND datum <= date('now')
                ORDER BY datum
            ''', (od, do)).fetchall()

            sma = []
            if has_sma:
                sma = conn.execute('''
                    SELECT datum, pv_generation_kwh, pv_kwh_inv1, pv_kwh_inv2, autarky_rate
                    FROM sma_dnevna
                    WHERE datum BETWEEN ? AND ?
                    ORDER BY datum
                ''', (od, do)).fetchall()

            return jsonify({
                'res': 'day', 'od': od, 'do': do,
                'hep': [dict(r) for r in hep],
                'sma': [dict(r) for r in sma],
            })
    finally:
        conn.close()



# ===== POSTAVKE API =====

@app.route('/api/postavke', methods=['GET', 'POST'])
def api_postavke():
    """Čitanje i pisanje konfiguracije iz .env datoteke"""
    env_path = os.path.join(os.path.dirname(__file__), '.env')

    # Sigurni ključevi koje smijemo čitati/pisati (bez lozinki u GET)
    safe_keys = ['HEP_USERNAME', 'HEP_SIFRA', 'SMA_USERNAME', 'SMA_PLANT_ID',
                 'SMA_INV1_ID', 'SMA_INV2_ID', 'HA_URL', 'TARIFA_VT', 'TARIFA_NT',
                 'TARIFA_PROD', 'TARIFA_PDV', 'TARIFA_VT_OD', 'TARIFA_VT_DO']
    all_keys = safe_keys + ['HEP_PASSWORD', 'SMA_PASSWORD', 'HA_TOKEN',
                            'DASHBOARD_PASSWORD', 'SMA_CLIENT_ID', 'DB_PATH', 'SECRET_KEY']

    if request.method == 'GET':
        cfg = {}
        for key in safe_keys:
            cfg[key] = os.environ.get(key, '')
        # Označi je li lozinka postavljena
        cfg['HEP_PASSWORD_SET']      = bool(os.environ.get('HEP_PASSWORD'))
        cfg['SMA_PASSWORD_SET']      = bool(os.environ.get('SMA_PASSWORD'))
        cfg['HA_TOKEN_SET']          = bool(os.environ.get('HA_TOKEN'))
        cfg['DASHBOARD_PASSWORD_SET']= bool(os.environ.get('DASHBOARD_PASSWORD'))
        return jsonify(cfg)

    elif request.method == 'POST':
        data = request.get_json()

        # Čitaj postojeći .env
        existing = {}
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        existing[k.strip()] = v.strip()

        # Ažuriraj samo dostavljene ključeve
        for key in all_keys:
            if key in data and data[key] != '':
                existing[key] = data[key]
            elif key in data and data[key] == '' and key.endswith('_PASSWORD'):
                pass  # Ne briši lozinku ako je prazna

        # Zapiši .env
        lines = ['# HEP Energy Monitor konfiguracija\n']
        sections = {
            'HEP': ['HEP_USERNAME', 'HEP_PASSWORD', 'HEP_SIFRA'],
            'SMA': ['SMA_USERNAME', 'SMA_PASSWORD', 'SMA_CLIENT_ID',
                    'SMA_PLANT_ID', 'SMA_INV1_ID', 'SMA_INV2_ID'],
            'HA':  ['HA_URL', 'HA_TOKEN'],
            'APP': ['DASHBOARD_PASSWORD', 'DB_PATH', 'SECRET_KEY'],
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

        # Reload env varijabli u trenutni proces
        for k, v in existing.items():
            os.environ[k] = v

        return jsonify({'ok': True})


@app.route('/api/postavke/status')
def api_postavke_status():
    """Status sustava — zadnji sync, broj zapisa, verzija"""
    conn = get_db()
    try:
        # Broj zapisa po tablici
        tables = {}
        for tbl in ['ocitanja_15min', 'ocitanja_satna', 'ocitanja_dnevna',
                    'sma_15min', 'sma_live', 'sma_dnevna', 'racuni']:
            try:
                n = conn.execute(f'SELECT COUNT(*) FROM {tbl}').fetchone()[0]
                tables[tbl] = n
            except:
                tables[tbl] = None

        # Zadnji HEP zapis
        hep_last = conn.execute(
            'SELECT MAX(ts) FROM ocitanja_satna'
        ).fetchone()[0]

        # Zadnji SMA live
        sma_last = None
        try:
            sma_last = conn.execute(
                'SELECT MAX(ts) FROM sma_live'
            ).fetchone()[0]
        except: pass

        # Raspon HEP podataka
        hep_range = conn.execute(
            'SELECT MIN(datum), MAX(datum) FROM ocitanja_dnevna'
        ).fetchone()

        # Raspon SMA podataka
        sma_range = (None, None)
        try:
            sma_range = conn.execute(
                'SELECT MIN(ts), MAX(ts) FROM sma_15min'
            ).fetchone()
        except: pass

        # Veličina baze
        db_size = 0
        try:
            db_size = os.path.getsize(os.environ.get('DB_PATH', '/data/hep_energy.db'))
        except: pass

        return jsonify({
            'version': '1.0.0',
            'tables': tables,
            'hep_last_sync': hep_last,
            'sma_last_sync': sma_last,
            'hep_range': {'od': hep_range[0], 'do': hep_range[1]},
            'sma_range': {'od': sma_range[0], 'do': sma_range[1]},
            'db_size_mb': round(db_size / 1024 / 1024, 2),
            'ts': datetime.now().isoformat(),
        })
    finally:
        conn.close()


@app.route('/api/postavke/korisnici', methods=['GET', 'POST', 'DELETE'])
def api_korisnici():
    """Upravljanje korisnicima u SQLite bazi s hashiranom lozinkom (salt:sha256)"""
    conn = get_db()
    try:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS korisnici (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                uloga TEXT DEFAULT 'viewer',
                aktivan INTEGER DEFAULT 1,
                stvoren TEXT DEFAULT (datetime('now')),
                zadnja_prijava TEXT
            )
        ''')
        conn.commit()

        if request.method == 'GET':
            rows = conn.execute(
                'SELECT id, username, uloga, aktivan, stvoren, zadnja_prijava FROM korisnici'
            ).fetchall()
            # Nikad ne vraćamo password_hash!
            return jsonify([dict(r) for r in rows])

        elif request.method == 'POST':
            d = request.get_json()
            username = d.get('username', '').strip()
            password = d.get('password', '')
            uloga    = d.get('uloga', 'viewer')

            if not username or not password:
                return jsonify({'ok': False, 'error': 'Korisnik i lozinka su obavezni'})

            # Salt + SHA256
            salt    = secrets.token_hex(16)
            pw_hash = hashlib.sha256(f'{salt}:{password}'.encode()).hexdigest()
            stored  = f'{salt}:{pw_hash}'

            conn.execute('''
                INSERT OR REPLACE INTO korisnici (username, password_hash, uloga)
                VALUES (?, ?, ?)
            ''', (username, stored, uloga))
            conn.commit()
            return jsonify({'ok': True})

        elif request.method == 'DELETE':
            username = request.args.get('username', '')
            # Ne dozvoli brisanje zadnjeg admina
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


@app.route('/api/postavke/backup')
def api_backup():
    """Download backup baze"""
    import shutil
    from flask import send_file
    import tempfile
    try:
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        shutil.copy2(DB_PATH, tmp.name)
        tmp.close()
        datum = datetime.now().strftime('%Y%m%d_%H%M')
        return send_file(tmp.name, as_attachment=True,
                        download_name=f'hep_energy_backup_{datum}.db',
                        mimetype='application/octet-stream')
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/postavke/backup/auto', methods=['POST'])
def api_backup_auto():
    """Automatski backup baze na disk"""
    import shutil
    backup_dir = os.path.join(os.path.dirname(DB_PATH), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    datum = datetime.now().strftime('%Y%m%d_%H%M')
    backup_path = os.path.join(backup_dir, f'hep_energy_{datum}.db')
    try:
        shutil.copy2(DB_PATH, backup_path)
        # Zadrži samo zadnjih 7 backupa
        backups = sorted([f for f in os.listdir(backup_dir) if f.endswith('.db')])
        for old in backups[:-7]:
            os.remove(os.path.join(backup_dir, old))
        return jsonify({'ok': True, 'path': backup_path, 'n_backups': len(backups)})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})



def api_import_hep():
    """Pokreni HEP import"""
    import subprocess
    dani = request.args.get('dani', '30')
    try:
        result = subprocess.run(
            ['python3', '/app/hep_scraper.py', '--dani', str(dani)],
            capture_output=True, text=True, timeout=300
        )
        lines = (result.stdout + result.stderr).strip().split('\n')
        info = lines[-1] if lines else 'Gotovo'
        return jsonify({'ok': True, 'info': info})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/setup/import-sma', methods=['POST'])
def api_import_sma():
    """Pokreni SMA history import"""
    import subprocess
    try:
        result = subprocess.run(
            ['python3', '/app/sma_history_import.py'],
            capture_output=True, text=True, timeout=600
        )
        lines = (result.stdout + result.stderr).strip().split('\n')
        info = lines[-1] if lines else 'Gotovo'
        return jsonify({'ok': True, 'info': info})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/api/setup/sync-ha', methods=['POST'])
def api_sync_ha():
    """Pokreni HA sync"""
    import subprocess
    try:
        result = subprocess.run(
            ['python3', '/app/ha_sender.py'],
            capture_output=True, text=True, timeout=60
        )
        lines = (result.stdout + result.stderr).strip().split('\n')
        info = lines[-1] if lines else 'Gotovo'
        return jsonify({'ok': True, 'info': info})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)