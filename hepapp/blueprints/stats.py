"""/api/stats/* + /api/stats/mjesecni + /api/stats/vt-kalibracija"""

from flask import Blueprint, jsonify, request

from ..db import get_config, get_db
from ..tariff import HEP_TARIFA, izracunaj_racun

bp = Blueprint('stats', __name__, url_prefix='/api/stats')


@bp.route('/usporedba')
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


@bp.route('/optimalno')
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


@bp.route('/mjesecni')
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
                r['sma_pv']        = sma['pv'] if sma else None
                r['sma_autarkija'] = sma['autarkija'] if sma else None
            result.append(r)

        racuni = [dict(r) for r in conn.execute(
            'SELECT * FROM racuni ORDER BY period DESC'
        ).fetchall()]

        return jsonify({
            'mjeseci': result,
            'tarifa':  HEP_TARIFA,
            'racuni':  racuni,
        })
    finally:
        conn.close()


@bp.route('/vt-kalibracija')
def api_vt_kalibracija():
    """Izračunaj stvarni VT udio iz HEP satnih očitanja.

    Vraća dva izračuna:
      - simple:  VT = ure unutar [VT_OD, VT_DO), svi dani
      - workdays: VT = ure unutar [VT_OD, VT_DO), ponedjeljak-petak

    Query params:
      mjesec=YYYY-MM   (default = zadnja 3 mjeseca)
      vt_od / vt_do    (default iz config: TARIFA_VT_OD, TARIFA_VT_DO)
    """
    mjesec = request.args.get('mjesec')
    try:
        vt_od = int(request.args.get('vt_od', get_config('TARIFA_VT_OD', '7')))
        vt_do = int(request.args.get('vt_do', get_config('TARIFA_VT_DO', '21')))
    except (TypeError, ValueError):
        vt_od, vt_do = 7, 21

    # Bounds: VT je sat ∈ [vt_od, vt_do - 1]
    vt_hi = vt_do - 1

    conn = get_db()
    try:
        if mjesec:
            where = "WHERE substr(ts,1,7) = ?"
            params = (mjesec,)
            group  = ""
            limit  = ""
        else:
            where = "WHERE ts >= datetime('now','-12 months')"
            params = ()
            group  = "GROUP BY substr(ts,1,7)"
            limit  = "ORDER BY mj DESC LIMIT 12"

        sql = f'''
            SELECT substr(ts,1,7) as mj,
                   ROUND(SUM(CASE WHEN CAST(strftime('%H', ts) AS INT) BETWEEN ? AND ?
                              THEN kwh_plus ELSE 0 END), 2) as vt_simple,
                   ROUND(SUM(CASE WHEN CAST(strftime('%w', ts) AS INT) BETWEEN 1 AND 5
                                   AND CAST(strftime('%H', ts) AS INT) BETWEEN ? AND ?
                              THEN kwh_plus ELSE 0 END), 2) as vt_workdays,
                   ROUND(SUM(kwh_plus), 2) as ukupno
            FROM ocitanja_satna
            {where}
            {group}
            {limit}
        '''
        rows = conn.execute(sql, (vt_od, vt_hi, vt_od, vt_hi) + params).fetchall()

        out = []
        for r in rows:
            uk = r['ukupno'] or 0
            vs = r['vt_simple'] or 0
            vw = r['vt_workdays'] or 0
            out.append({
                'mjesec':           r['mj'],
                'ukupno_kwh':       uk,
                'vt_simple_kwh':    vs,
                'nt_simple_kwh':    round(uk - vs, 2),
                'vt_simple_perc':   round(100 * vs / uk, 1) if uk else None,
                'vt_workdays_kwh':  vw,
                'nt_workdays_kwh':  round(uk - vw, 2),
                'vt_workdays_perc': round(100 * vw / uk, 1) if uk else None,
            })

        return jsonify({
            'vt_od': vt_od, 'vt_do': vt_do,
            'mjeseci': out,
            'objasnjenje': {
                'simple':   f'VT = svi dani u satima {vt_od}-{vt_do}h',
                'workdays': f'VT = pon–pet u satima {vt_od}-{vt_do}h, vikend cijeli NT',
            },
        })
    finally:
        conn.close()
