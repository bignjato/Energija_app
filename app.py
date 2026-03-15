#!/usr/bin/env python3
"""
Flask web server za HEP + SMA energy dashboard
"""

from flask import Flask, jsonify, render_template_string, request
import sqlite3
import os
from datetime import datetime, timedelta

app = Flask(__name__)
DB_PATH = os.environ.get('DB_PATH', '/data/hep_energy.db')
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), 'dashboard_template.html')

# Stvarne HEP cijene iz računa (s PDV 13%)
# Od 01.01.2026 (HEPI bijeli, E-K-N-BIJ1)
HEP_TARIFA = {
    'vt_opskrba':  0.131205,  # €/kWh VT opskrba (bez PDV)
    'nt_opskrba':  0.064379,  # €/kWh NT opskrba (bez PDV)
    'vt_distrib':  0.044446,  # €/kWh VT distribucija
    'nt_distrib':  0.020514,  # €/kWh NT distribucija
    'vt_prijenos': 0.021256,  # €/kWh VT prijenos
    'nt_prijenos': 0.008175,  # €/kWh NT prijenos
    'solidarna':   0.003982,  # €/kWh solidarna naknada
    'oie':         0.013239,  # €/kWh OIE naknada
    'opskrbna_mj': 0.982,     # €/mj opskrbna naknada
    'mjerna_mj':   1.983,     # €/mj naknada za mjernu uslugu
    'pdv':         0.13,      # PDV 13%
    'vt_udio':     0.45,      # pretpostavljeni udio VT (45%)
    'otkup':       0.064379,  # €/kWh otkup viška (NT cijena)
}

def izracunaj_racun(kwh_plus, kwh_minus, n_dana=30):
    """
    Procjena HEP računa na temelju stvarnih cijena.
    kwh_plus = potrošnja iz mreže (A+)
    kwh_minus = predaja u mrežu (A-)
    """
    t = HEP_TARIFA
    vt = kwh_plus * t['vt_udio']
    nt = kwh_plus * (1 - t['vt_udio'])
    n_mj = n_dana / 30.0

    # Opskrba
    opskrba = (vt * t['vt_opskrba'] + nt * t['nt_opskrba'] +
               kwh_plus * (t['solidarna'] + t['oie']) +
               t['opskrbna_mj'] * n_mj -
               kwh_minus * t['otkup'])  # odbitak za predaju

    # Mreža (distribucija + prijenos)
    mreza = (vt * (t['vt_distrib'] + t['vt_prijenos']) +
             nt * (t['nt_distrib'] + t['nt_prijenos']) +
             t['mjerna_mj'] * n_mj)

    osnovica = opskrba + mreza
    pdv = osnovica * t['pdv']
    return round(osnovica + pdv, 2)


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
from flask import session

app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))
LOGIN_PASSWORD = os.environ.get('DASHBOARD_PASSWORD', '')

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if LOGIN_PASSWORD and not session.get('logged_in'):
            if request.path.startswith('/api/'):
                return jsonify({'error': 'Unauthorized'}), 401
            return '''<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Login — HEP Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#080c12;color:#d0dde8;font-family:IBM Plex Sans,sans-serif;
     display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{background:#0f1623;border:1px solid #1f2d3d;border-radius:12px;padding:40px;width:360px}
h1{font-size:20px;color:#fff;margin-bottom:6px}
p{font-size:13px;color:#526070;margin-bottom:24px}
input{width:100%;padding:10px 14px;background:#080c12;border:1px solid #1f2d3d;
      border-radius:8px;color:#d0dde8;font-size:14px;margin-bottom:14px;font-family:monospace}
input:focus{outline:none;border-color:#22d3ee}
button{width:100%;padding:11px;background:#22d3ee;color:#000;border:none;
       border-radius:8px;font-size:14px;font-weight:600;cursor:pointer}
.err{color:#f87171;font-size:13px;margin-bottom:12px;display:none}
</style></head>
<body><div class="box">
<h1>⚡ HEP Energy Monitor</h1>
<p>Unesite lozinku za pristup</p>
<div class="err" id="err">Pogrešna lozinka</div>
<form method="POST" action="/login">
<input type="password" name="password" placeholder="Lozinka" autofocus>
<button type="submit">Prijava</button>
</form>
</div>
<script>
if(window.location.search.includes('err'))
  document.getElementById('err').style.display='block'
</script>
</body></html>'''
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['POST'])
def do_login():
    pw = request.form.get('password', '')
    if not LOGIN_PASSWORD or pw == LOGIN_PASSWORD:
        session['logged_in'] = True
        return redirect('/')
    return redirect('/login?err=1')


@app.route('/login')
def login_page():
    return redirect('/?login=1')


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')


from flask import redirect

# Wrap glavnih ruta s login_required
_orig_index = app.view_functions['index']
app.view_functions['index'] = login_required(_orig_index)


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


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)
