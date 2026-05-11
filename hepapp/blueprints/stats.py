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


@bp.route('/procjena-trenutni')
def api_procjena_trenutni():
    """Procjena računa za TEKUĆI mjesec na temelju dosadašnje potrošnje.

    Logika:
      - Sumiraj potrošnju/predaju od 1. do danas iz ocitanja_satna
      - Računaj prorate na cijeli mjesec (× n_dana_mjesec / proteklo_dana)
      - Primijeni HEP tarife + VT/NT slider + pretplate
      - Vrati: proteklo, projicirano za mjesec, postotak proteklog mjeseca
    """
    from datetime import datetime
    from calendar import monthrange
    from ..tariff import HEP_TARIFA, get_vt_udio, izracunaj_racun

    now = datetime.now()
    god, mj = now.year, now.month
    n_dana_mj = monthrange(god, mj)[1]
    proteklo_dana = now.day  # uključno do današnjeg dana

    conn = get_db()
    try:
        try:
            vt_od = int(get_config('TARIFA_VT_OD', '7'))
            vt_do = int(get_config('TARIFA_VT_DO', '21'))
        except (TypeError, ValueError):
            vt_od, vt_do = 7, 21
        vt_hi = vt_do - 1

        mj_str = f'{god:04d}-{mj:02d}'
        row = conn.execute(f'''
            SELECT
                ROUND(SUM(kwh_plus), 2) as kwh_plus,
                ROUND(SUM(kwh_minus), 2) as kwh_minus,
                ROUND(SUM(CASE WHEN CAST(strftime('%H', ts) AS INT) BETWEEN ? AND ?
                            THEN kwh_plus ELSE 0 END), 2) as kwh_vt,
                ROUND(SUM(CASE WHEN CAST(strftime('%H', ts) AS INT) BETWEEN ? AND ?
                            THEN 0 ELSE kwh_plus END), 2) as kwh_nt,
                MAX(ts) as zadnji_ts,
                MIN(ts) as prvi_ts
            FROM ocitanja_satna
            WHERE substr(ts,1,7) = ?
        ''', (vt_od, vt_hi, vt_od, vt_hi, mj_str)).fetchone()

        # zadnji uvezeni račun za pretplatu (ako postoji)
        last_bill = conn.execute('''
            SELECT pretplata, mjerna_mjernina FROM racuni
            WHERE pretplata IS NOT NULL
            ORDER BY datum_racuna DESC LIMIT 1
        ''').fetchone()
    finally:
        conn.close()

    kp = row['kwh_plus'] or 0
    km = row['kwh_minus'] or 0
    kvt = row['kwh_vt'] or 0
    knt = row['kwh_nt'] or 0
    # broj dana s podacima (dani s ts u tekućem mjesecu)
    try:
        prvi = row['prvi_ts']
        zadnji = row['zadnji_ts']
        if prvi and zadnji:
            dan1 = int(prvi[8:10])
            danN = int(zadnji[8:10])
            dani_s_podacima = max(1, danN - dan1 + 1)
        else:
            dani_s_podacima = 1
    except Exception:
        dani_s_podacima = max(1, proteklo_dana)

    # Procjena (prorata)
    faktor = n_dana_mj / dani_s_podacima
    proj_kp = round(kp * faktor, 2)
    proj_km = round(km * faktor, 2)

    # Stvarni VT udio za sad
    vt_udio_real = (kvt / kp) if kp > 0 else get_vt_udio()

    # Trošak: do sad
    proteklo_racun = izracunaj_racun(kp, km, dani_s_podacima, vt_udio=vt_udio_real)
    # Procjena za cijeli mjesec
    proj_racun = izracunaj_racun(proj_kp, proj_km, n_dana_mj, vt_udio=vt_udio_real)

    # Pretplate (fiksni dio)
    pret_opskrba = (last_bill['pretplata'] if last_bill else None) or HEP_TARIFA['opskrbna_mj']
    pret_mjerna  = (last_bill['mjerna_mjernina'] if last_bill else None) or HEP_TARIFA['mjerna_mj']
    fiksno = round(pret_opskrba + pret_mjerna, 2)

    return jsonify({
        'mjesec':              mj_str,
        'n_dana_mjesec':       n_dana_mj,
        'dani_s_podacima':     dani_s_podacima,
        'proteklo_dana':       proteklo_dana,
        'postotak_mjeseca':    round(100 * dani_s_podacima / n_dana_mj, 1),
        'kwh_plus_proteklo':   kp,
        'kwh_minus_proteklo':  km,
        'kwh_vt_proteklo':     kvt,
        'kwh_nt_proteklo':     knt,
        'kwh_plus_procjena':   proj_kp,
        'kwh_minus_procjena':  proj_km,
        'racun_proteklo':      proteklo_racun,
        'racun_procjena':      proj_racun,
        'fiksne_naknade':      fiksno,
        'vt_udio_stvarni':     round(100 * vt_udio_real, 1),
        'tarifa': {
            'vt_kwh': round(HEP_TARIFA['vt_opskrba'] + HEP_TARIFA['vt_distrib'] + HEP_TARIFA['vt_prijenos'], 6),
            'nt_kwh': round(HEP_TARIFA['nt_opskrba'] + HEP_TARIFA['nt_distrib'] + HEP_TARIFA['nt_prijenos'], 6),
            'pretplata': fiksno,
            'pdv':       HEP_TARIFA['pdv'],
            'otkup':     HEP_TARIFA['otkup'],
        },
    })


@bp.route('/peak-snaga')
def api_peak_snaga():
    """Vršna snaga iz 15-min očitanja.

    kW = kwh_plus(15min) * 4  (jer je period 15 min = 1/4h)
    Vraća peak za zadnji dan, tjedan, mjesec, godinu + timestamp peak-a.
    """
    conn = get_db()
    try:
        out = {}
        for label, period in [
            ('dan',      "datetime('now','-1 days')"),
            ('tjedan',   "datetime('now','-7 days')"),
            ('mjesec',   "datetime('now','-30 days')"),
            ('godina',   "datetime('now','-365 days')"),
            ('uvijek',   "datetime('1970-01-01')"),
        ]:
            row = conn.execute(f'''
                SELECT ts, ROUND(kwh_plus * 4, 2) as kw, ROUND(kwh_minus * 4, 2) as kw_predaja
                FROM ocitanja_15min
                WHERE ts >= {period} AND ts <= datetime('now')
                ORDER BY kwh_plus DESC LIMIT 1
            ''').fetchone()
            row_pred = conn.execute(f'''
                SELECT ts, ROUND(kwh_minus * 4, 2) as kw
                FROM ocitanja_15min
                WHERE ts >= {period} AND ts <= datetime('now') AND kwh_minus > 0
                ORDER BY kwh_minus DESC LIMIT 1
            ''').fetchone()
            out[label] = {
                'peak_potrosnja_kw': row['kw'] if row else None,
                'peak_potrosnja_ts': row['ts'] if row else None,
                'peak_predaja_kw':   row_pred['kw'] if row_pred else None,
                'peak_predaja_ts':   row_pred['ts'] if row_pred else None,
            }
        return jsonify(out)
    finally:
        conn.close()


@bp.route('/vt-nt-trosak')
def api_vt_nt_trosak():
    """VT/NT trošak split (€) — koliko po tarifama u zadnjih 12 mj.

    Koristi config VT_OD/VT_DO i tarifne cijene iz HEP_TARIFA.
    """
    from ..tariff import HEP_TARIFA
    try:
        vt_od = int(get_config('TARIFA_VT_OD', '7'))
        vt_do = int(get_config('TARIFA_VT_DO', '21'))
    except (TypeError, ValueError):
        vt_od, vt_do = 7, 21
    vt_hi = vt_do - 1

    conn = get_db()
    try:
        rows = conn.execute(f'''
            SELECT substr(ts,1,7) as mj,
                   ROUND(SUM(CASE WHEN CAST(strftime('%H', ts) AS INT) BETWEEN ? AND ?
                              THEN kwh_plus ELSE 0 END), 2) as vt_kwh,
                   ROUND(SUM(CASE WHEN CAST(strftime('%H', ts) AS INT) BETWEEN ? AND ?
                              THEN 0 ELSE kwh_plus END), 2) as nt_kwh,
                   ROUND(SUM(kwh_minus), 2) as predaja_kwh
            FROM ocitanja_satna
            WHERE ts >= datetime('now','-12 months')
            GROUP BY substr(ts,1,7)
            ORDER BY mj DESC
        ''', (vt_od, vt_hi, vt_od, vt_hi)).fetchall()

        t = HEP_TARIFA
        out = []
        for r in rows:
            vt_kwh = r['vt_kwh'] or 0
            nt_kwh = r['nt_kwh'] or 0
            pred   = r['predaja_kwh'] or 0
            # neto trošak po tarifi (opskrba + mreža); bez fiksnih naknada i PDV
            vt_eur = round(vt_kwh * (t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos']), 2)
            nt_eur = round(nt_kwh * (t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos']), 2)
            pred_eur = round(pred * t['otkup'], 2)
            uk_kwh = vt_kwh + nt_kwh
            out.append({
                'mjesec':       r['mj'],
                'vt_kwh':       vt_kwh,
                'nt_kwh':       nt_kwh,
                'predaja_kwh':  pred,
                'vt_eur':       vt_eur,
                'nt_eur':       nt_eur,
                'predaja_eur':  pred_eur,
                'vt_perc':      round(100 * vt_kwh / uk_kwh, 1) if uk_kwh else None,
                'neto_trosak':  round(vt_eur + nt_eur - pred_eur, 2),
            })

        return jsonify({
            'vt_od': vt_od, 'vt_do': vt_do,
            'mjeseci': out,
            'tarife': {
                'vt_kwh_eur': round(t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos'], 6),
                'nt_kwh_eur': round(t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos'], 6),
                'otkup':      t['otkup'],
            },
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
