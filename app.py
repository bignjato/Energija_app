#!/usr/bin/env python3
"""
Flask web server za HEP + SMA energy dashboard
InfoBot — Boris Ignjatović
"""

import functools
import hashlib
import hmac
import io
import logging
import os
import secrets
import shutil
import sqlite3
import subprocess
from datetime import datetime, timedelta

from flask import Flask, jsonify, request, session, redirect, send_file
from werkzeug.middleware.proxy_fix import ProxyFix

try:
    from flask_limiter import Limiter
    from flask_limiter.util import get_remote_address
    HAS_LIMITER = True
except ImportError:
    HAS_LIMITER = False

# ===== APP / CONFIG =====
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'dashboard_template.html')

_secret = os.environ.get('SECRET_KEY', '').strip()
if not _secret or _secret in ('promijenite-ovo', 'change-me'):
    raise RuntimeError(
        'SECRET_KEY nije postavljen u .env (ili je placeholder). '
        'Postavi 64-hex random key (python -c "import secrets; print(secrets.token_hex(32))").'
    )
if len(_secret) < 32:
    raise RuntimeError(f'SECRET_KEY prekratak ({len(_secret)} chars). Treba ≥32.')
app.secret_key = _secret

app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', '1') != '0'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 86400 * 30

LOGIN_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '')
PBKDF2_ITERATIONS = 200_000

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = app.logger

# Rate limiter (graceful skip ako nije instaliran)
if HAS_LIMITER:
    limiter = Limiter(
        app=app,
        key_func=get_remote_address,
        default_limits=[],
        storage_uri='memory://',
    )
else:
    log.warning('flask-limiter nije instaliran — rate limiting onemogućen.')
    limiter = None


# ===== PASSWORD HELPERS =====

def hash_password(pw: str) -> str:
    """PBKDF2-SHA256 hash. Format: pbkdf2$<iter>$<salt>$<hash>."""
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), PBKDF2_ITERATIONS).hex()
    return f'pbkdf2${PBKDF2_ITERATIONS}${salt}${h}'


def verify_password(pw: str, stored: str) -> bool:
    """Provjeri lozinku — podržava PBKDF2 i legacy formate."""
    if not stored:
        return False
    try:
        if stored.startswith('pbkdf2$'):
            _, iter_s, salt, h = stored.split('$', 3)
            test = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), int(iter_s)).hex()
            return hmac.compare_digest(test, h)
        if ':' in stored:
            salt, h = stored.split(':', 1)
            test = hashlib.sha256(f'{salt}:{pw}'.encode()).hexdigest()
            return hmac.compare_digest(test, h)
        test = hashlib.sha256(pw.encode()).hexdigest()
        return hmac.compare_digest(test, stored)
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    return not (stored or '').startswith('pbkdf2$')


# ===== AUTH DECORATORS =====

def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get('logged_in'):
            return jsonify({'error': 'Unauthorized'}), 401
        return fn(*a, **kw)
    return wrapper


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get('logged_in'):
            return jsonify({'error': 'Unauthorized'}), 401
        if session.get('uloga') != 'admin':
            return jsonify({'error': 'Forbidden — admin only'}), 403
        return fn(*a, **kw)
    return wrapper


# ===== BAZA =====

def init_db():
    """Inicijaliziraj bazu — sheme, defaultovi, WAL mode."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA synchronous=NORMAL')
    conn.execute('PRAGMA foreign_keys=ON')

    conn.execute('''
        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated TEXT DEFAULT (datetime('now'))
        )
    ''')
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

    count = conn.execute('SELECT COUNT(*) FROM korisnici').fetchone()[0]
    if count == 0:
        initial_pw = os.environ.get('INITIAL_ADMIN_PASSWORD', 'admin')
        conn.execute(
            'INSERT INTO korisnici (username, password_hash, uloga) VALUES (?, ?, ?)',
            ('admin', hash_password(initial_pw), 'admin'),
        )
        log.warning('Kreiran defaultni admin korisnik. PROMIJENI LOZINKU ODMAH.')

    migrated = conn.execute("SELECT value FROM config WHERE key='_migrated'").fetchone()
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
                    'INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)', (key, val)
                )
        conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('_migrated', '1')")
        log.info('Migriran .env u config tablicu')

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
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute('SELECT value FROM config WHERE key=?', (key,)).fetchone()
        conn.close()
        return row[0] if row else os.environ.get(key, default)
    except Exception:
        return os.environ.get(key, default)


def set_config(key, value):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        'INSERT OR REPLACE INTO config (key, value, updated) VALUES (?, ?, datetime("now"))',
        (key, value),
    )
    conn.commit()
    conn.close()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA busy_timeout=5000')
    return conn


with app.app_context():
    try:
        init_db()
    except Exception as e:
        log.exception(f'Init DB error: {e}')


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


# ===== TEMPLATE CACHE =====
_DASHBOARD_CACHE = None


def get_dashboard_html():
    global _DASHBOARD_CACHE
    if _DASHBOARD_CACHE is None:
        with open(TEMPLATE_PATH, 'r', encoding='utf-8') as f:
            _DASHBOARD_CACHE = f.read()
    return _DASHBOARD_CACHE


# ===== LOGIN UI =====
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


# ===== CSRF (origin check) =====

def _csrf_check():
    """Provjeri Origin/Referer za state-changing requestove."""
    if request.method not in ('POST', 'PUT', 'DELETE', 'PATCH'):
        return None
    origin = request.headers.get('Origin') or request.headers.get('Referer') or ''
    host = request.host_url.rstrip('/')
    if not origin or not origin.startswith(host):
        return jsonify({'error': 'CSRF check failed'}), 403
    return None


@app.before_request
def check_login():
    """Provjeri login + CSRF prije svake rute."""
    free = ['/login', '/logout', '/health', '/favicon.ico']
    if request.path in free or request.path.startswith('/static/'):
        return None
    if not session.get('logged_in'):
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Unauthorized'}), 401
        return LOGIN_PAGE
    return _csrf_check()


# ===== RUTE: PUBLIC =====

@app.route('/health')
def health():
    return 'OK', 200


@app.route('/')
def index():
    return get_dashboard_html()


# ===== RUTE: LOGIN =====

def _do_login(username: str, pw: str) -> bool:
    """Postavi sesiju ako su credentialsi valjani. Vraća True/False."""
    if LOGIN_PASSWORD and hmac.compare_digest(pw, LOGIN_PASSWORD):
        session['logged_in'] = True
        session['username'] = username or 'admin'
        session['uloga'] = 'admin'
        session.permanent = True
        return True
    try:
        conn = get_db()
        user = conn.execute(
            'SELECT id, username, password_hash, uloga FROM korisnici WHERE username=? AND aktivan=1',
            (username,),
        ).fetchone()
        conn.close()
        if user and verify_password(pw, user['password_hash']):
            session['logged_in'] = True
            session['username'] = user['username']
            session['uloga'] = user['uloga']
            session.permanent = True
            # rehash legacy hash
            if needs_rehash(user['password_hash']):
                try:
                    c2 = get_db()
                    c2.execute(
                        'UPDATE korisnici SET password_hash=? WHERE id=?',
                        (hash_password(pw), user['id']),
                    )
                    c2.commit()
                    c2.close()
                except Exception as e:
                    log.warning(f'rehash failed: {e}')
            try:
                c2 = get_db()
                c2.execute(
                    'UPDATE korisnici SET zadnja_prijava=datetime("now") WHERE username=?',
                    (user['username'],),
                )
                c2.commit()
                c2.close()
            except Exception:
                pass
            return True
    except Exception as e:
        log.exception(f'Login error: {e}')
    return False


def _login_route():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        pw = request.form.get('password', '')
        if _do_login(username, pw):
            return redirect('/')
        return LOGIN_PAGE.replace('display:none', 'display:block'), 401
    return LOGIN_PAGE


if HAS_LIMITER:
    _login_route = limiter.limit('10 per minute; 30 per hour', methods=['POST'])(_login_route)

app.add_url_rule('/login', 'login_page', _login_route, methods=['GET', 'POST'])


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/login')


# ===== RUTE: API DATA =====

@app.route('/api/data')
def api_data():
    conn = get_db()
    try:
        satna = conn.execute('''
            SELECT ts, kwh_plus, kwh_minus
            FROM ocitanja_satna
            WHERE ts <= datetime('now') AND kwh_plus > 0
            ORDER BY ts DESC LIMIT 168
        ''').fetchall()

        dnevna = conn.execute('''
            SELECT datum, kwh_plus, kwh_minus
            FROM ocitanja_dnevna
            WHERE datum <= date('now')
            ORDER BY datum DESC LIMIT 90
        ''').fetchall()

        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_live'"
        ).fetchone() is not None

        sma_live = None
        sma_dnevna = []
        sma_satna = []
        if has_sma:
            row = conn.execute('''
                SELECT ts, pv_generation_w, feed_in_w, external_consumption_w,
                       total_consumption_w, direct_consumption_w, autarky_rate, self_consumption_rate
                FROM sma_live ORDER BY ts DESC LIMIT 1
            ''').fetchone()
            if row:
                sma_live = dict(row)
            sma_dnevna = [dict(r) for r in conn.execute('''
                SELECT datum, pv_generation_kwh, feed_in_kwh, grid_consumption_kwh,
                       total_consumption_kwh, self_consumption_kwh, autarky_rate
                FROM sma_dnevna ORDER BY datum DESC LIMIT 90
            ''').fetchall()]
            sma_satna = [dict(r) for r in conn.execute('''
                SELECT strftime('%Y-%m-%dT%H:00:00', ts) as sat,
                       ROUND(AVG(pv_generation_w), 0) as pv_w,
                       ROUND(AVG(feed_in_w), 0) as feed_w,
                       ROUND(AVG(external_consumption_w), 0) as grid_w,
                       ROUND(AVG(total_consumption_w), 0) as total_w
                FROM sma_live
                WHERE ts >= datetime('now', '-7 days') AND ts <= datetime('now')
                GROUP BY strftime('%Y-%m-%dT%H:00:00', ts)
                ORDER BY sat DESC LIMIT 168
            ''').fetchall()]

        tarifa = None
        has_tarife = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tarife'"
        ).fetchone() is not None
        if has_tarife:
            row = conn.execute(
                'SELECT * FROM tarife WHERE aktivan=1 ORDER BY id DESC LIMIT 1'
            ).fetchone()
            if row:
                tarifa = dict(row)

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


@app.route('/api/data/sve')
def api_data_sve():
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
    od  = request.args.get('od',  (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%d'))
    do  = request.args.get('do',  datetime.now().strftime('%Y-%m-%d'))
    res = request.args.get('res', 'day')

    conn = get_db()
    try:
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_dnevna'"
        ).fetchone() is not None
        has_15min = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_15min'"
        ).fetchone() is not None

        if res == 'hour':
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
            return jsonify({'res': 'hour', 'od': od, 'do': do,
                            'hep': [dict(r) for r in hep],
                            'sma': [dict(r) for r in sma]})

        if res == 'week':
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
            return jsonify({'res': 'week', 'od': od, 'do': do,
                            'hep': [dict(r) for r in hep],
                            'sma': [dict(r) for r in sma]})

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
        return jsonify({'res': 'day', 'od': od, 'do': do,
                        'hep': [dict(r) for r in hep],
                        'sma': [dict(r) for r in sma]})
    finally:
        conn.close()


@app.route('/api/tarifa', methods=['GET', 'POST'])
def api_tarifa():
    conn = get_db()
    try:
        if request.method == 'POST':
            data = request.get_json() or {}
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
    conn = get_db()
    try:
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_dnevna'"
        ).fetchone() is not None
        if not has_sma:
            return jsonify({'error': 'Nema SMA podataka'})
        rows = conn.execute('''
            SELECT h.datum,
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
    conn = get_db()
    try:
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_live'"
        ).fetchone() is not None

        hep_satno = conn.execute('''
            SELECT CAST(strftime('%H', ts) AS INTEGER) as sat,
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
                SELECT CAST(strftime('%H', ts) AS INTEGER) as sat,
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
                SELECT CAST(strftime('%H', ts) AS INTEGER) as sat,
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
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM sma_live ORDER BY ts DESC LIMIT 1').fetchone()
        if row:
            return jsonify(dict(row))
        return jsonify({'error': 'Nema podataka'})
    finally:
        conn.close()


@app.route('/api/stats/mjesecni')
def api_mjesecni():
    conn = get_db()
    try:
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_dnevna'"
        ).fetchone() is not None

        rows = conn.execute('''
            SELECT substr(h.datum,1,7) as mjesec,
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
    """CRUD HEP računa."""
    if request.method != 'GET' and session.get('uloga') != 'admin':
        return jsonify({'error': 'Forbidden — admin only'}), 403
    conn = get_db()
    try:
        if request.method == 'GET':
            rows = conn.execute('SELECT * FROM racuni ORDER BY period DESC').fetchall()
            return jsonify([dict(r) for r in rows])
        if request.method == 'POST':
            d = request.get_json() or {}
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
        # DELETE
        period = request.args.get('period')
        if period:
            conn.execute('DELETE FROM racuni WHERE period=?', (period,))
            conn.commit()
        return jsonify({'ok': True})
    finally:
        conn.close()


# ===== RUTE: POSTAVKE (admin) =====

@app.route('/api/postavke', methods=['GET', 'POST'])
@admin_required
def api_postavke():
    env_path = os.path.join(os.path.dirname(__file__), '.env')
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
        return jsonify(cfg)

    data = request.get_json() or {}
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
    os.chmod(env_path, 0o600)

    for k, v in existing.items():
        os.environ[k] = v

    # Napomena: sync kontejner i dalje koristi stari .env do restarta.
    return jsonify({
        'ok': True,
        'warning': 'Promijene će se primijeniti tek nakon restarta sync kontejnera.',
    })


@app.route('/api/postavke/status')
def api_postavke_status():
    conn = get_db()
    try:
        # Whitelist tablica — sigurno za f-string jer su hardkodirane
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
            'version': '1.1.0',
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


@app.route('/api/postavke/backup')
@admin_required
def api_backup():
    """Download backup baze (in-memory, bez tempfile)."""
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


@app.route('/api/postavke/backup/auto', methods=['POST'])
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


# ===== RUTE: SETUP (admin) =====
#
# NAPOMENA: scraperi (hep_scraper.py, sma_scraper.py, sma_history_import.py,
# ha_sender.py) žive u SYNC kontejneru. Web kontejner ih ne vidi preko
# subprocess. Ove rute zadržane su radi UI-ja, ali u trenutnoj topologiji
# samo signaliziraju potrebu za pokretanjem; stvarni rad obavlja sync_loop.sh.

def _run_scraper(args, timeout):
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        lines = (result.stdout + result.stderr).strip().split('\n')
        return {'ok': True, 'info': lines[-1] if lines else 'Gotovo'}
    except Exception as e:
        return {'ok': False, 'error': str(e)}


@app.route('/api/setup/import-hep', methods=['POST'])
@admin_required
def api_import_hep():
    dani = request.args.get('dani', '30')
    try:
        n = int(dani)
        if n < 1 or n > 3650:
            raise ValueError
    except ValueError:
        return jsonify({'ok': False, 'error': 'dani mora biti broj 1–3650'}), 400
    return jsonify(_run_scraper(['python3', '/app/hep_scraper.py', '--dani', str(n)], 300))


@app.route('/api/setup/import-sma', methods=['POST'])
@admin_required
def api_import_sma():
    return jsonify(_run_scraper(['python3', '/app/sma_history_import.py'], 600))


@app.route('/api/setup/sync-ha', methods=['POST'])
@admin_required
def api_sync_ha():
    return jsonify(_run_scraper(['python3', '/app/ha_sender.py'], 60))


# ===== ENTRYPOINT =====

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
