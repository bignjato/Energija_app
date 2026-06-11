"""/api/stats/* + /api/stats/mjesecni + /api/stats/vt-kalibracija"""

from flask import Blueprint, jsonify, request

from ..db import get_config, get_db
from ..tariff import HEP_TARIFA, izracunaj_racun

bp = Blueprint('stats', __name__, url_prefix='/api/stats')


@bp.route('/usporedba')
def api_usporedba():
    from datetime import datetime as _dt
    conn = get_db()
    try:
        has_sma_dnevna = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_dnevna'"
        ).fetchone() is not None
        has_sma_15min = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_15min'"
        ).fetchone() is not None
        has_sma_live = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sma_live'"
        ).fetchone() is not None

        if has_sma_dnevna:
            # UNION datuma -> red postoji ako BILO KOJA tablica ima taj datum
            # (jedan scraper moze kasniti, drugi ne).
            rows = conn.execute("""
                WITH datumi AS (
                    SELECT datum FROM ocitanja_dnevna WHERE datum >= date('now','-90 days')
                    UNION
                    SELECT datum FROM sma_dnevna       WHERE datum >= date('now','-90 days')
                )
                SELECT d.datum,
                       h.kwh_plus              as hep_potrosnja,
                       h.kwh_minus             as hep_predaja,
                       s.pv_generation_kwh     as sma_proizvodnja,
                       s.feed_in_kwh           as sma_predaja,
                       s.grid_consumption_kwh  as sma_mreza,
                       s.total_consumption_kwh as sma_potrosnja,
                       s.autarky_rate
                FROM datumi d
                LEFT JOIN ocitanja_dnevna h ON h.datum = d.datum
                LEFT JOIN sma_dnevna       s ON s.datum = d.datum
                ORDER BY d.datum
            """).fetchall()
        else:
            rows = conn.execute("""
                SELECT datum,
                       kwh_plus  as hep_potrosnja,
                       kwh_minus as hep_predaja,
                       NULL as sma_proizvodnja, NULL as sma_predaja,
                       NULL as sma_mreza, NULL as sma_potrosnja,
                       NULL as autarky_rate
                FROM ocitanja_dnevna
                WHERE datum >= date('now', '-90 days')
                ORDER BY datum
            """).fetchall()

        result = [dict(r) for r in rows]
        today_str = _dt.now().strftime('%Y-%m-%d')
        today_row = next((r for r in result if r['datum'] == today_str), None)

        # HEP "danas" iz satne tablice ako dnevni red nedostaje/prazan
        hep_today = conn.execute("""
            SELECT ROUND(COALESCE(SUM(kwh_plus),  0), 3) as kp,
                   ROUND(COALESCE(SUM(kwh_minus), 0), 3) as km,
                   COUNT(*) as n
            FROM ocitanja_satna
            WHERE date(ts) = ?
        """, (today_str,)).fetchone()
        has_hep_today = bool(hep_today and hep_today['n'])
        hep_kp = hep_today['kp'] if hep_today else 0
        hep_km = hep_today['km'] if hep_today else 0

        # SMA "danas" iz sma_15min (preferirano) ili sma_live
        sma_today = None
        if has_sma_15min:
            t = conn.execute("""
                SELECT ROUND(SUM(pv_generation_wh)   /1000.0, 3) as pv,
                       ROUND(SUM(feed_in_wh)         /1000.0, 3) as feed,
                       ROUND(SUM(grid_consumption_wh)/1000.0, 3) as grid,
                       ROUND(SUM(total_consumption_wh)/1000.0, 3) as cons,
                       COUNT(*) as n
                FROM sma_15min
                WHERE date(ts) = ?
            """, (today_str,)).fetchone()
            if t and t['n']:
                cons = t['cons'] or 0
                grid = t['grid'] or 0
                sma_today = {
                    'sma_proizvodnja': t['pv']   or 0,
                    'sma_predaja':     t['feed'] or 0,
                    'sma_mreza':       grid,
                    'sma_potrosnja':   cons,
                    'autarky_rate':    round(1 - grid/cons, 4) if cons > 0 else None,
                }
        if sma_today is None and has_sma_live:
            t = conn.execute("""
                SELECT COUNT(*) as n,
                       MIN(ts) as ts_min, MAX(ts) as ts_max,
                       AVG(pv_generation_w)        as avg_pv,
                       AVG(feed_in_w)              as avg_feed,
                       AVG(external_consumption_w) as avg_grid,
                       AVG(total_consumption_w)    as avg_cons,
                       AVG(autarky_rate)           as avg_aut
                FROM sma_live WHERE date(ts) = ?
            """, (today_str,)).fetchone()
            if t and t['n']:
                try:
                    t1 = _dt.fromisoformat((t['ts_min'] or '').rstrip('Z'))
                    t2 = _dt.fromisoformat((t['ts_max'] or '').rstrip('Z'))
                    proteklo_h = max((t2 - t1).total_seconds() / 3600.0, 1/12)
                except Exception:
                    proteklo_h = t['n'] / 12.0
                sma_today = {
                    'sma_proizvodnja': round((t['avg_pv']   or 0) * proteklo_h / 1000, 3),
                    'sma_predaja':     round((t['avg_feed'] or 0) * proteklo_h / 1000, 3),
                    'sma_mreza':       round((t['avg_grid'] or 0) * proteklo_h / 1000, 3),
                    'sma_potrosnja':   round((t['avg_cons'] or 0) * proteklo_h / 1000, 3),
                    'autarky_rate':    round(t['avg_aut'] or 0, 4),
                }

        if has_hep_today or sma_today:
            if today_row is None:
                today_row = {
                    'datum': today_str,
                    'hep_potrosnja': None, 'hep_predaja': None,
                    'sma_proizvodnja': None, 'sma_predaja': None,
                    'sma_mreza': None, 'sma_potrosnja': None,
                    'autarky_rate': None,
                }
                result.append(today_row)
            today_row['_realtime'] = True
            if has_hep_today:
                if not today_row.get('hep_potrosnja'):
                    today_row['hep_potrosnja'] = hep_kp
                if not today_row.get('hep_predaja'):
                    today_row['hep_predaja'] = hep_km
            if sma_today:
                for k, v in sma_today.items():
                    if today_row.get(k) in (None, 0):
                        today_row[k] = v

        return jsonify(result)
    finally:
        conn.close()



@bp.route('/registri')
def api_registri():
    """Sluzbena HEP stanja brojila po tarifnim registrima + mjesecne delte.

    Registri: A+_T1 (potrosnja VT), A+_T2 (potrosnja NT),
              A-_T1 (predaja VT),  A-_T2 (predaja NT).
    Delta dva uzastopna mjesecna stanja = sluzbeni mjesecni kWh po tarifi.
    """
    conn = get_db()
    try:
        has_reg = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='hep_registri'"
        ).fetchone() is not None
        if not has_reg:
            return jsonify({'stanja': [], 'mjeseci': []})

        rows = conn.execute("""
            SELECT datum, obis, value FROM hep_registri
            ORDER BY datum, obis
        """).fetchall()

        # pivot: datum -> {obis: value}
        stanja = {}
        for r in rows:
            stanja.setdefault(r['datum'], {})[r['obis']] = r['value']

        datumi = sorted(stanja.keys())
        out_stanja = [
            {'datum': d, **{k.replace('+', 'p').replace('-', 'm'): v
                            for k, v in stanja[d].items()}}
            for d in datumi
        ]

        # mjesecne delte izmedju uzastopnih stanja
        mjeseci = []
        for prev, cur in zip(datumi, datumi[1:]):
            s0, s1 = stanja[prev], stanja[cur]
            def delta(key):
                a, b = s0.get(key), s1.get(key)
                if a is None or b is None:
                    return None
                d = round(b - a, 2)
                return d if d >= 0 else None  # zamjena brojila i sl.
            vt  = delta('A+_T1')
            nt  = delta('A+_T2')
            pvt = delta('A-_T1')
            pnt = delta('A-_T2')
            uk  = (vt or 0) + (nt or 0)
            mjeseci.append({
                'mjesec':       cur[:7],
                'od': prev, 'do': cur,
                'vt_kwh': vt, 'nt_kwh': nt,
                'pred_vt_kwh': pvt, 'pred_nt_kwh': pnt,
                'ukupno_kwh':  round(uk, 2) if (vt is not None or nt is not None) else None,
                'predaja_kwh': round((pvt or 0) + (pnt or 0), 2)
                               if (pvt is not None or pnt is not None) else None,
                'vt_perc': round(100 * vt / uk, 1) if vt is not None and uk > 0 else None,
            })
        mjeseci.reverse()

        return jsonify({'stanja': out_stanja, 'mjeseci': mjeseci})
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
    from ..tariff import (
        HEP_TARIFA, get_vt_udio, izracunaj_racun,
        izracunaj_racun_bijeli, otkup_viskova_bijeli,
    )

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
                ROUND(SUM(CASE WHEN CAST(strftime('%H', ts) AS INT) BETWEEN ? AND ?
                            THEN kwh_minus ELSE 0 END), 2) as kwh_vt_pred,
                ROUND(SUM(CASE WHEN CAST(strftime('%H', ts) AS INT) BETWEEN ? AND ?
                            THEN 0 ELSE kwh_minus END), 2) as kwh_nt_pred,
                MAX(ts) as zadnji_ts,
                MIN(ts) as prvi_ts
            FROM ocitanja_satna
            WHERE substr(ts,1,7) = ?
        ''', (vt_od, vt_hi, vt_od, vt_hi,
              vt_od, vt_hi, vt_od, vt_hi, mj_str)).fetchone()

        # zadnji uvezeni račun za pretplatu i model tarife
        last_bill = conn.execute('''
            SELECT pretplata, mjerna_mjernina, model_tarife FROM racuni
            WHERE pretplata IS NOT NULL OR model_tarife IS NOT NULL
            ORDER BY datum_racuna DESC LIMIT 1
        ''').fetchone()
    finally:
        conn.close()

    kp = row['kwh_plus'] or 0
    km = row['kwh_minus'] or 0
    kvt = row['kwh_vt'] or 0
    knt = row['kwh_nt'] or 0
    kvt_pred = row['kwh_vt_pred'] or 0
    knt_pred = row['kwh_nt_pred'] or 0
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

    # Detekcija tarifnog modela
    model_str = (last_bill['model_tarife'] if last_bill else '') or ''
    is_bijeli = 'bijeli' in model_str.lower() or 'BIJ' in model_str.upper()

    proj_kvt = round(kvt * faktor, 2)
    proj_knt = round(knt * faktor, 2)
    proj_kvt_pred = round(kvt_pred * faktor, 2)
    proj_knt_pred = round(knt_pred * faktor, 2)

    if is_bijeli:
        # HEPI bijeli — net metering
        det_proteklo = izracunaj_racun_bijeli(kvt, knt, kvt_pred, knt_pred, dani_s_podacima)
        det_proj     = izracunaj_racun_bijeli(proj_kvt, proj_knt, proj_kvt_pred, proj_knt_pred, n_dana_mj)
        otkup_proteklo = otkup_viskova_bijeli(kvt, knt, kvt_pred, knt_pred)
        otkup_proj     = otkup_viskova_bijeli(proj_kvt, proj_knt, proj_kvt_pred, proj_knt_pred)
        proteklo_racun = det_proteklo['iznos']
        proj_racun     = det_proj['iznos']
        model_label    = 'HEPI bijeli (net-metering)'
    else:
        proteklo_racun = izracunaj_racun(kp, km, dani_s_podacima, vt_udio=vt_udio_real)
        proj_racun     = izracunaj_racun(proj_kp, proj_km, n_dana_mj, vt_udio=vt_udio_real)
        det_proteklo = det_proj = None
        otkup_proteklo = otkup_proj = None
        model_label = 'Standardni dvotarifni'

    # Pretplate (fiksni dio)
    pret_opskrba = (last_bill['pretplata'] if last_bill else None) or HEP_TARIFA['opskrbna_mj']
    pret_mjerna  = (last_bill['mjerna_mjernina'] if last_bill else None) or HEP_TARIFA['mjerna_mj']
    fiksno = round(pret_opskrba + pret_mjerna, 2)

    return jsonify({
        'mjesec':              mj_str,
        'model':               model_label,
        'n_dana_mjesec':       n_dana_mj,
        'dani_s_podacima':     dani_s_podacima,
        'proteklo_dana':       proteklo_dana,
        'postotak_mjeseca':    round(100 * dani_s_podacima / n_dana_mj, 1),
        'kwh_plus_proteklo':   kp,
        'kwh_minus_proteklo':  km,
        'kwh_vt_proteklo':     kvt,
        'kwh_nt_proteklo':     knt,
        'kwh_vt_pred_proteklo': kvt_pred,
        'kwh_nt_pred_proteklo': knt_pred,
        'kwh_plus_procjena':   proj_kp,
        'kwh_minus_procjena':  proj_km,
        'racun_proteklo':      proteklo_racun,
        'racun_procjena':      proj_racun,
        'fiksne_naknade':      fiksno,
        'vt_udio_stvarni':     round(100 * vt_udio_real, 1),
        'bijeli_proteklo':     det_proteklo,
        'bijeli_procjena':     det_proj,
        'otkup_proteklo':      otkup_proteklo,
        'otkup_procjena':      otkup_proj,
        'tarifa': {
            'vt_kwh': round(HEP_TARIFA['vt_opskrba'] + HEP_TARIFA['vt_distrib'] + HEP_TARIFA['vt_prijenos'], 6),
            'nt_kwh': round(HEP_TARIFA['nt_opskrba'] + HEP_TARIFA['nt_distrib'] + HEP_TARIFA['nt_prijenos'], 6),
            'pretplata':  fiksno,
            'pdv':        HEP_TARIFA['pdv'],
            'otkup':      HEP_TARIFA['otkup'],
            'vt_otkup':   HEP_TARIFA['vt_otkup'],
            'nt_otkup':   HEP_TARIFA['nt_otkup'],
        },
    })


@bp.route('/cijene')
def api_cijene():
    """Stvarne aktivne cijene za prikaz korisniku.

    Izvori (po prioritetu):
      1. Zadnji uvezeni HEP račun (pretplata, mjerna_mjernina)
      2. HEP_TARIFA konstante (VT/NT/distrib/prijenos/solidarna/oie/PDV/otkup)
      3. VT_UDIO_PERC iz config-a
    """
    from ..tariff import HEP_TARIFA, get_vt_udio
    conn = get_db()
    try:
        row = conn.execute('''
            SELECT pretplata, mjerna_mjernina, datum_racuna, period, model_tarife
            FROM racuni
            WHERE pretplata IS NOT NULL OR mjerna_mjernina IS NOT NULL
            ORDER BY datum_racuna DESC, period DESC LIMIT 1
        ''').fetchone()
    finally:
        conn.close()

    t = HEP_TARIFA
    pretplata_opskrba = (row['pretplata'] if row else None) or t['opskrbna_mj']
    pretplata_mjerna  = (row['mjerna_mjernina'] if row else None) or t['mjerna_mj']
    vt_udio = get_vt_udio()

    # Puna cijena VT/NT po kWh (uključujući sve sastavnice + PDV)
    vt_full = (t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos']
               + t['solidarna'] + t['oie']) * (1 + t['pdv'])
    nt_full = (t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos']
               + t['solidarna'] + t['oie']) * (1 + t['pdv'])
    avg_full = vt_full * vt_udio + nt_full * (1 - vt_udio)

    return jsonify({
        'izvor':              row['period'] + ' (' + (row['model_tarife'] or '') + ')' if row else 'default HEP_TARIFA',
        'pretplata_opskrba':  round(pretplata_opskrba, 3),
        'pretplata_mjerna':   round(pretplata_mjerna, 3),
        'pretplata_ukupno':   round(pretplata_opskrba + pretplata_mjerna, 3),
        'vt_opskrba':         t['vt_opskrba'],
        'nt_opskrba':         t['nt_opskrba'],
        'vt_distrib':         t['vt_distrib'],
        'nt_distrib':         t['nt_distrib'],
        'vt_prijenos':        t['vt_prijenos'],
        'nt_prijenos':        t['nt_prijenos'],
        'solidarna':          t['solidarna'],
        'oie':                t['oie'],
        'pdv':                t['pdv'],
        'otkup':              t['otkup'],
        'vt_otkup':           t['vt_otkup'],
        'nt_otkup':           t['nt_otkup'],
        'vt_udio_perc':       round(vt_udio * 100, 1),
        'vt_full_eur_kwh':    round(vt_full, 6),
        'nt_full_eur_kwh':    round(nt_full, 6),
        'avg_full_eur_kwh':   round(avg_full, 6),
    })


@bp.route('/weather')
def api_weather():
    """Open-Meteo passthrough za lokaciju postrojenja.

    Config keys: PV_LAT, PV_LON (default = Odra/Lukavec).
    """
    import requests
    lat = float(get_config('PV_LAT', '45.732') or 45.732)
    lon = float(get_config('PV_LON', '16.087') or 16.087)
    try:
        r = requests.get(
            'https://api.open-meteo.com/v1/forecast',
            params={
                'latitude': lat,
                'longitude': lon,
                'current': 'temperature_2m,cloud_cover,weather_code,is_day,wind_speed_10m',
                'daily': 'weather_code,temperature_2m_max,temperature_2m_min,cloud_cover_mean,precipitation_sum,sunshine_duration',
                'forecast_days': 7,
                'timezone': 'auto',
            },
            timeout=8,
        )
        if r.status_code != 200:
            return jsonify({'error': f'open-meteo HTTP {r.status_code}'}), 502
        return jsonify(r.json())
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@bp.route('/power-limit-history')
def api_power_limit_history():
    """24h history inverter power limit-a iz HA."""
    import os, requests, urllib3
    from datetime import datetime, timedelta
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    ent = get_config('HA_ENT_POWER_LIMIT', '')
    if not ent:
        return jsonify({'series': [], 'note': 'HA_ENT_POWER_LIMIT nije konfiguriran'})
    ha_url = os.environ.get('HA_URL', '').strip()
    ha_tok = os.environ.get('HA_TOKEN', '').strip()
    if not ha_url or not ha_tok:
        return jsonify({'series': [], 'note': 'HA_URL/HA_TOKEN nisu postavljeni'})

    start = (datetime.utcnow() - timedelta(hours=24)).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    try:
        r = requests.get(
            f"{ha_url.rstrip('/')}/api/history/period/{start}",
            headers={'Authorization': f'Bearer {ha_tok}'},
            params={'filter_entity_id': ent, 'minimal_response': 'true'},
            timeout=15, verify=False,
        )
        if r.status_code != 200:
            return jsonify({'series': [], 'note': f'HA HTTP {r.status_code}'})
        data = r.json()
        if not data or not data[0]:
            return jsonify({'series': []})
        rows = data[0]
        series = []
        for s in rows:
            ts = s.get('last_changed') or s.get('last_updated')
            val = s.get('state')
            if val in (None, 'unknown', 'unavailable'):
                continue
            try:
                series.append({'ts': ts, 'w': float(val)})
            except ValueError:
                pass
        return jsonify({'series': series, 'entity': ent})
    except Exception as e:
        return jsonify({'series': [], 'note': str(e)})


@bp.route('/plant-info')
def api_plant_info():
    """Plant metapodaci + agregati za live dashboard widgets.

    Vraća (s SunnyPortal-feature parity):
      - nominal_kw, commission_date  (iz config)
      - co2_today_kg, co2_total_kg   (PV × CO2_FACTOR_G_KWH)
      - reimbursement_today_eur, reimbursement_total_eur (PV × otkup)
      - sufficiency_perc, consumption_perc  (live iz sma_live)
      - pv_today_kwh, pv_month_kwh, pv_year_kwh, pv_total_kwh
      - power_limit_w, power_limit_perc  (iz HA ako konfigurirano)
    """
    from datetime import datetime
    from ..tariff import HEP_TARIFA
    import os, requests, urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    nominal_kw     = float(get_config('PV_NOMINAL_KW', '0') or 0)
    commission     = get_config('PV_COMMISSION_DATE', '')
    co2_factor     = float(get_config('PV_CO2_FACTOR_G_KWH', '640') or 640)
    feed_tariff    = float(get_config('PV_FEED_TARIFF_EUR', '') or HEP_TARIFA.get('otkup', 0.064379))
    power_limit_ent = get_config('HA_ENT_POWER_LIMIT', '')

    today_str = datetime.now().strftime('%Y-%m-%d')
    mj_str    = datetime.now().strftime('%Y-%m')
    god_str   = datetime.now().strftime('%Y')

    conn = get_db()
    try:
        pv_today = conn.execute(
            "SELECT pv_generation_kwh FROM sma_dnevna WHERE datum=?", (today_str,)
        ).fetchone()
        pv_today = pv_today['pv_generation_kwh'] if pv_today and pv_today['pv_generation_kwh'] else 0

        pv_month = conn.execute(
            "SELECT ROUND(SUM(pv_generation_kwh),2) as s FROM sma_dnevna WHERE substr(datum,1,7)=?",
            (mj_str,)
        ).fetchone()['s'] or 0

        pv_year = conn.execute(
            "SELECT ROUND(SUM(pv_generation_kwh),2) as s FROM sma_dnevna WHERE substr(datum,1,4)=?",
            (god_str,)
        ).fetchone()['s'] or 0

        pv_total = conn.execute(
            "SELECT ROUND(SUM(pv_generation_kwh),2) as s FROM sma_dnevna"
        ).fetchone()['s'] or 0

        live = conn.execute(
            "SELECT pv_generation_w, feed_in_w, total_consumption_w, external_consumption_w, "
            "autarky_rate, self_consumption_rate FROM sma_live ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    # Power limit iz HA (ako konfiguriran)
    power_limit_w = None
    power_limit_perc = None
    if power_limit_ent:
        try:
            ha_url = os.environ.get('HA_URL', '').strip()
            ha_tok = os.environ.get('HA_TOKEN', '').strip()
            if ha_url and ha_tok:
                r = requests.get(
                    f"{ha_url.rstrip('/')}/api/states/{power_limit_ent}",
                    headers={'Authorization': f'Bearer {ha_tok}'},
                    timeout=8, verify=False,
                )
                if r.status_code == 200:
                    state = r.json().get('state')
                    if state not in (None, 'unknown', 'unavailable'):
                        power_limit_w = float(state)
                        if nominal_kw > 0:
                            power_limit_perc = round(100 * power_limit_w / (nominal_kw * 1000), 1)
        except Exception:
            pass

    return jsonify({
        'nominal_kw':           nominal_kw,
        'commission_date':      commission,
        'co2_today_kg':         round(pv_today * co2_factor / 1000, 1),
        'co2_total_kg':         round(pv_total * co2_factor / 1000, 1),
        'reimbursement_today_eur': round(pv_today * feed_tariff, 2),
        'reimbursement_total_eur': round(pv_total * feed_tariff, 2),
        'pv_today_kwh':         pv_today,
        'pv_month_kwh':         pv_month,
        'pv_year_kwh':          pv_year,
        'pv_total_kwh':         pv_total,
        'live': dict(live) if live else None,
        'sufficiency_perc':     round((live['autarky_rate'] or 0) * 100, 1) if live and live['autarky_rate'] is not None else None,
        'consumption_perc':     round((live['self_consumption_rate'] or 0) * 100, 1) if live and live['self_consumption_rate'] is not None else None,
        'power_limit_w':        power_limit_w,
        'power_limit_perc':     power_limit_perc,
        'co2_factor_g_kwh':     co2_factor,
        'feed_tariff':          feed_tariff,
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
