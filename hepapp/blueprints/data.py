"""/api/data, /api/data/sve, /api/povijest, /api/sma/live"""

import re
from datetime import timedelta

from flask import Blueprint, jsonify, request

from ..db import get_db
from ..tariff import get_tarifa, get_vt_udio, is_bijeli
from ..timeutil import (
    SQL_NOW_LOCAL, SQL_NOW_UTC, SQL_TODAY_LOCAL, now_local, sql_now_utc,
    today_local, utc_raspon_dana,
)

bp = Blueprint('data', __name__, url_prefix='/api')

_DATUM_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _omm_filter(prefix=''):
    """Vrati (sql_clause, params) za filtriranje po mjernom mjestu.

    Čita ?omm= iz querya. Prazno ili 'all' → bez filtra (agregat svih OMM-ova).
    `prefix` je alias tablice ('h.' ili '') za kvalificiranje kolone.
    """
    omm = (request.args.get('omm') or '').strip()
    if not omm or omm == 'all':
        return '', []
    return f"AND {prefix}mjerno_mjesto = ?", [omm]


@bp.route('/omm')
def api_omm():
    """Lista mjernih mjesta za selektor + zadano (s najviše podataka)."""
    conn = get_db()
    try:
        if conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='mjerna_mjesta'"
        ).fetchone() is None:
            return jsonify({'omm': [], 'zadano': None})
        rows = conn.execute('''
            SELECT m.id, m.naziv, m.adresa, m.tip,
                   (SELECT COUNT(*) FROM ocitanja_dnevna d WHERE d.mjerno_mjesto = m.id) as n_dana
            FROM mjerna_mjesta m
            ORDER BY n_dana DESC, m.id
        ''').fetchall()
        omm = [dict(r) for r in rows]
        return jsonify({'omm': omm, 'zadano': omm[0]['id'] if omm else None})
    finally:
        conn.close()


def _efektivne_cijene() -> dict:
    """Izračunaj prosječnu cijenu kupnje/prodaje iz važećeg cjenika + VT udio.

    Koristi se u UI-u Financije za jednostavne projekcije.
    """
    t = get_tarifa()
    vt_udio = get_vt_udio()
    vt_full = (t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos']
               + t['solidarna'] + t['oie'])
    nt_full = (t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos']
               + t['solidarna'] + t['oie'])
    kupnja_avg = (vt_full * vt_udio + nt_full * (1 - vt_udio)) * (1 + t['pdv'])
    # Model: zadnji uvezeni račun pa config TARIFA_MODEL — isto kao /api/stats/*
    bijeli = is_bijeli()
    if bijeli:
        prodaja = t['vt_otkup'] * vt_udio + t['nt_otkup'] * (1 - vt_udio)
    else:
        prodaja = t['otkup']
    return {
        'kupnja_avg':  round(kupnja_avg, 6),
        'prodaja':     round(prodaja, 6),
        'vt_udio':     round(vt_udio * 100, 1),
        'model':       'bijeli' if bijeli else 'standardni',
    }


@bp.route('/data')
def api_data():
    omm_c, omm_p = _omm_filter()
    conn = get_db()
    try:
        satna = conn.execute(f'''
            SELECT ts, SUM(kwh_plus) as kwh_plus, SUM(kwh_minus) as kwh_minus
            FROM ocitanja_satna
            WHERE ts <= {SQL_NOW_LOCAL}
              AND (kwh_plus > 0 OR kwh_minus > 0) {omm_c}
            GROUP BY ts
            ORDER BY ts DESC LIMIT 168
        ''', omm_p).fetchall()

        dnevna = conn.execute(f'''
            SELECT datum, SUM(kwh_plus) as kwh_plus, SUM(kwh_minus) as kwh_minus
            FROM ocitanja_dnevna
            WHERE datum <= {SQL_TODAY_LOCAL} {omm_c}
            GROUP BY datum
            ORDER BY datum DESC LIMIT 90
        ''', omm_p).fetchall()

        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_live'"
        ).fetchone() is not None

        sma_live, sma_dnevna, sma_satna = None, [], []
        if has_sma:
            # ORDER BY id (monotonično rastući) umjesto ts — različiti pisači
            # koriste različite TZ formate pa ts može imati lex-ordering issue.
            row = conn.execute('''
                SELECT ts, pv_generation_w, feed_in_w, external_consumption_w,
                       total_consumption_w, direct_consumption_w, autarky_rate, self_consumption_rate
                FROM sma_live ORDER BY id DESC LIMIT 1
            ''').fetchone()
            if row:
                sma_live = dict(row)
            sma_dnevna = [dict(r) for r in conn.execute('''
                SELECT datum, pv_generation_kwh, feed_in_kwh, grid_consumption_kwh,
                       total_consumption_kwh, self_consumption_kwh, autarky_rate
                FROM sma_dnevna ORDER BY datum DESC LIMIT 90
            ''').fetchall()]
            # sma_live.ts je UTC ('...+00:00') -> granice u UTC 'T' formatu,
            # a oznaka sata lokalna (kao HEP satna serija uz koju se crta).
            sma_satna = [dict(r) for r in conn.execute(f'''
                SELECT strftime('%Y-%m-%dT%H:00:00', ts, 'localtime') as sat,
                       ROUND(AVG(pv_generation_w), 0) as pv_w,
                       ROUND(AVG(feed_in_w), 0) as feed_w,
                       ROUND(AVG(external_consumption_w), 0) as grid_w,
                       ROUND(AVG(total_consumption_w), 0) as total_w
                FROM sma_live
                WHERE ts >= {sql_now_utc('-7 days')} AND ts <= {SQL_NOW_UTC}
                GROUP BY strftime('%Y-%m-%dT%H:00:00', ts, 'localtime')
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

        # Danas zbirno iz sma_live (jer sma_dnevna ne piše tijekom dana)
        sma_today = None
        if has_sma:
            today_str = today_local()
            # lokalni "danas" kao UTC raspon (ts je UTC) — ujedno koristi indeks
            utc_od, utc_do = utc_raspon_dana(today_str)
            t = conn.execute('''
                SELECT
                    COUNT(*) as n,
                    MIN(ts) as ts_min,
                    MAX(ts) as ts_max,
                    AVG(feed_in_w) as avg_feed,
                    AVG(total_consumption_w) as avg_cons,
                    AVG(external_consumption_w) as avg_grid,
                    AVG(pv_generation_w) as avg_pv,
                    AVG(autarky_rate) as avg_aut
                FROM sma_live
                WHERE ts >= ? AND ts < ?
            ''', (utc_od, utc_do)).fetchone()
            if t and t['n']:
                # protekla aktivna sati od prvog sample-a do zadnjeg
                try:
                    from datetime import datetime as _dt
                    t1 = _dt.fromisoformat(t['ts_min'].rstrip('Z'))
                    t2 = _dt.fromisoformat(t['ts_max'].rstrip('Z'))
                    proteklo_h = max((t2 - t1).total_seconds() / 3600.0, 1/12)
                except Exception:
                    proteklo_h = t['n'] / 12.0  # ~5min sample → 12/h
                sma_today = {
                    'pv_generation_kwh':    round((t['avg_pv'] or 0) * proteklo_h / 1000, 2),
                    'feed_in_kwh':          round((t['avg_feed'] or 0) * proteklo_h / 1000, 2),
                    'total_consumption_kwh':round((t['avg_cons'] or 0) * proteklo_h / 1000, 2),
                    'grid_consumption_kwh': round((t['avg_grid'] or 0) * proteklo_h / 1000, 2),
                    'autarky_rate':         round((t['avg_aut'] or 0), 4),
                    'proteklo_h':           round(proteklo_h, 1),
                    'n_samples':            t['n'],
                }
            # Ako sma_dnevna za danas ima pv ali ne i feed/cons → preferiraj sma_dnevna pv
            today_row = next((r for r in sma_dnevna if r['datum'] == today_str), None)
            if today_row and today_row['pv_generation_kwh'] and sma_today:
                sma_today['pv_generation_kwh'] = today_row['pv_generation_kwh']

        return jsonify({
            'satna':      [dict(r) for r in reversed(satna)],
            'dnevna':     [dict(r) for r in reversed(dnevna)],
            'sma_live':   sma_live,
            'sma_dnevna': list(reversed(sma_dnevna)),
            'sma_satna':  list(reversed(sma_satna)),
            'sma_today':  sma_today,
            'tarifa':     tarifa,
            'efektivne_cijene': _efektivne_cijene(),
            'ts':         now_local().replace(tzinfo=None).isoformat(),
        })
    finally:
        conn.close()


@bp.route('/data/sve')
def api_data_sve():
    omm_c, omm_p = _omm_filter()
    conn = get_db()
    try:
        dnevna = conn.execute(f'''
            SELECT datum, SUM(kwh_plus) as kwh_plus, SUM(kwh_minus) as kwh_minus
            FROM ocitanja_dnevna
            WHERE datum <= {SQL_TODAY_LOCAL} {omm_c}
            GROUP BY datum
            ORDER BY datum DESC
        ''', omm_p).fetchall()
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
            'dnevna':     [dict(r) for r in dnevna],
            'sma_dnevna': [dict(r) for r in sma_dnevna],
        })
    finally:
        conn.close()


@bp.route('/povijest')
def api_povijest():
    od  = request.args.get('od',  (now_local() - timedelta(days=30)).strftime('%Y-%m-%d'))
    do  = request.args.get('do',  today_local())
    res = request.args.get('res', 'day')
    omm_c, omm_p = _omm_filter()

    conn = get_db()
    try:
        has_sma = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_dnevna'"
        ).fetchone() is not None
        has_15min = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_15min'"
        ).fetchone() is not None

        if res == 'hour':
            # Raspon po ts (koristi indeks) umjesto date(ts) BETWEEN; za
            # neispravan format datuma ostaje stari predikat.
            datumi_ok = bool(_DATUM_RE.match(od) and _DATUM_RE.match(do))
            if datumi_ok:
                hep_where, hep_p = 'ts >= ? AND ts <= ?', [od + 'T00:00:00', do + 'T23:59:59']
            else:
                hep_where, hep_p = 'date(ts) BETWEEN ? AND ?', [od, do]
            hep = conn.execute(f'''
                SELECT ts, SUM(kwh_plus) as kwh_plus, SUM(kwh_minus) as kwh_minus
                FROM ocitanja_satna
                WHERE {hep_where} AND ts <= {SQL_NOW_LOCAL} {omm_c}
                GROUP BY ts
                ORDER BY ts
            ''', hep_p + omm_p).fetchall()
            sma = []
            if has_15min:
                # sma_15min.ts je UTC ('Z'): lokalni dani -> UTC raspon, a sat
                # se označava lokalno da se poklopi s HEP ts ključem.
                try:
                    sma_where, sma_p = 'ts >= ? AND ts < ?', utc_raspon_dana(od, do)
                except ValueError:
                    sma_where, sma_p = "date(ts, 'localtime') BETWEEN ? AND ?", (od, do)
                sma = conn.execute(f'''
                    SELECT strftime('%Y-%m-%dT%H:00:00', ts, 'localtime') as sat,
                           ROUND(SUM(pv_w_total)*0.25/1000.0, 3) as pv_kwh,
                           ROUND(SUM(pv_w_inv1)*0.25/1000.0, 3) as inv1_kwh,
                           ROUND(SUM(pv_w_inv2)*0.25/1000.0, 3) as inv2_kwh
                    FROM sma_15min
                    WHERE {sma_where}
                    GROUP BY strftime('%Y-%m-%dT%H:00:00', ts, 'localtime')
                    ORDER BY sat
                ''', sma_p).fetchall()
            return jsonify({'res': 'hour', 'od': od, 'do': do,
                            'hep': [dict(r) for r in hep],
                            'sma': [dict(r) for r in sma]})

        if res == 'week':
            hep = conn.execute(f'''
                SELECT strftime('%Y-W%W', datum) as tjedan,
                       MIN(datum) as datum_od,
                       ROUND(SUM(kwh_plus), 2) as kwh_plus,
                       ROUND(SUM(kwh_minus), 2) as kwh_minus,
                       COUNT(DISTINCT datum) as n_dana
                FROM ocitanja_dnevna
                WHERE datum BETWEEN ? AND ? AND datum <= {SQL_TODAY_LOCAL} {omm_c}
                GROUP BY strftime('%Y-W%W', datum)
                ORDER BY tjedan
            ''', [od, do] + omm_p).fetchall()
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

        hep = conn.execute(f'''
            SELECT datum, SUM(kwh_plus) as kwh_plus, SUM(kwh_minus) as kwh_minus
            FROM ocitanja_dnevna
            WHERE datum BETWEEN ? AND ? AND datum <= {SQL_TODAY_LOCAL} {omm_c}
            GROUP BY datum
            ORDER BY datum
        ''', [od, do] + omm_p).fetchall()
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


@bp.route('/sma/live')
def api_sma_live():
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM sma_live ORDER BY id DESC LIMIT 1').fetchone()
        if row:
            return jsonify(dict(row))
        return jsonify({'error': 'Nema podataka'})
    finally:
        conn.close()
