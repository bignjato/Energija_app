"""/api/data, /api/data/sve, /api/povijest, /api/sma/live"""

from datetime import datetime, timedelta

from flask import Blueprint, jsonify, request

from ..db import get_db, get_config
from ..tariff import HEP_TARIFA, get_vt_udio

bp = Blueprint('data', __name__, url_prefix='/api')


def _efektivne_cijene() -> dict:
    """Izračunaj prosječnu cijenu kupnje/prodaje iz HEP_TARIFA + VT udio.

    Koristi se u UI-u Financije za jednostavne projekcije.
    """
    t = HEP_TARIFA
    vt_udio = get_vt_udio()
    vt_full = (t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos']
               + t['solidarna'] + t['oie'])
    nt_full = (t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos']
               + t['solidarna'] + t['oie'])
    kupnja_avg = (vt_full * vt_udio + nt_full * (1 - vt_udio)) * (1 + t['pdv'])
    # Detektira HEPI bijeli prema config-u TARIFA_MODEL ili default standardni
    model = (get_config('TARIFA_MODEL', 'standardni') or 'standardni').lower()
    if 'bijeli' in model:
        prodaja = t['vt_otkup'] * vt_udio + t['nt_otkup'] * (1 - vt_udio)
    else:
        prodaja = t['otkup']
    return {
        'kupnja_avg':  round(kupnja_avg, 6),
        'prodaja':     round(prodaja, 6),
        'vt_udio':     round(vt_udio * 100, 1),
        'model':       'bijeli' if 'bijeli' in model else 'standardni',
    }


@bp.route('/data')
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

        sma_live, sma_dnevna, sma_satna = None, [], []
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
            'satna':      [dict(r) for r in reversed(satna)],
            'dnevna':     [dict(r) for r in reversed(dnevna)],
            'sma_live':   sma_live,
            'sma_dnevna': list(reversed(sma_dnevna)),
            'sma_satna':  list(reversed(sma_satna)),
            'tarifa':     tarifa,
            'efektivne_cijene': _efektivne_cijene(),
            'ts':         datetime.now().isoformat(),
        })
    finally:
        conn.close()


@bp.route('/data/sve')
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
            'dnevna':     [dict(r) for r in dnevna],
            'sma_dnevna': [dict(r) for r in sma_dnevna],
        })
    finally:
        conn.close()


@bp.route('/povijest')
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


@bp.route('/sma/live')
def api_sma_live():
    conn = get_db()
    try:
        row = conn.execute('SELECT * FROM sma_live ORDER BY ts DESC LIMIT 1').fetchone()
        if row:
            return jsonify(dict(row))
        return jsonify({'error': 'Nema podataka'})
    finally:
        conn.close()
