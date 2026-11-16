"""/api/stats/* + /api/stats/mjesecni + /api/stats/vt-kalibracija"""

import logging
from datetime import date, datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

from ..db import get_config, get_db
from ..tariff import (
    _norm_period, get_tarifa, get_tarifa_izvor, is_bijeli, izracunaj_racun,
    izracunaj_racun_bijeli, otkup_viskova_bijeli, zadnji_racun_i_dug,
)
from ..timeutil import (
    SQL_NOW_LOCAL, SQL_NOW_UTC, TZ, now_local, raspon_mjeseca, sql_now_local,
    sql_today_local, today_local, utc_raspon_dana, vt_sql,
)

MODEL_NAZIV = {
    'bijeli':     'HEPI bijeli (net-metering)',
    'standardni': 'Standardni dvotarifni',
}

log = logging.getLogger(__name__)

bp = Blueprint('stats', __name__, url_prefix='/api/stats')


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


@bp.route('/usporedba')
def api_usporedba():
    from datetime import datetime as _dt
    conn = get_db()
    try:
        has_sma_dnevna = _table_exists(conn, 'sma_dnevna')
        has_sma_15min  = _table_exists(conn, 'sma_15min')
        has_sma_live   = _table_exists(conn, 'sma_live')

        if has_sma_dnevna:
            # UNION datuma -> red postoji ako BILO KOJA tablica ima taj datum
            # (jedan scraper moze kasniti, drugi ne).
            rows = conn.execute(f"""
                WITH datumi AS (
                    SELECT datum FROM ocitanja_dnevna WHERE datum >= {sql_today_local('-90 days')}
                    UNION
                    SELECT datum FROM sma_dnevna       WHERE datum >= {sql_today_local('-90 days')}
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
            rows = conn.execute(f"""
                SELECT datum,
                       kwh_plus  as hep_potrosnja,
                       kwh_minus as hep_predaja,
                       NULL as sma_proizvodnja, NULL as sma_predaja,
                       NULL as sma_mreza, NULL as sma_potrosnja,
                       NULL as autarky_rate
                FROM ocitanja_dnevna
                WHERE datum >= {sql_today_local('-90 days')}
                ORDER BY datum
            """).fetchall()

        result = [dict(r) for r in rows]
        today_str = today_local()
        today_row = next((r for r in result if r['datum'] == today_str), None)
        # SMA tablice imaju UTC ts -> lokalni "danas" kao UTC raspon
        utc_od, utc_do = utc_raspon_dana(today_str)

        # HEP "danas" iz satne tablice ako dnevni red nedostaje/prazan
        hep_today = conn.execute("""
            SELECT ROUND(COALESCE(SUM(kwh_plus),  0), 3) as kp,
                   ROUND(COALESCE(SUM(kwh_minus), 0), 3) as km,
                   COUNT(*) as n
            FROM ocitanja_satna
            WHERE ts >= ? AND ts <= ?
        """, (today_str + 'T00:00:00', today_str + 'T23:59:59')).fetchone()
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
                WHERE ts >= ? AND ts < ?
            """, (utc_od, utc_do)).fetchone()
            if t and t['n']:
                cons = t['cons'] or 0
                grid = t['grid'] or 0
                sma_today = {
                    'sma_proizvodnja': t['pv']   or 0,
                    'sma_predaja':     t['feed'] or 0,
                    'sma_mreza':       grid,
                    'sma_potrosnja':   cons,
                    # prag 0.1 kWh: pri zanemarivoj potrošnji autarkija je čisti šum
                    'autarky_rate':    round(1 - grid/cons, 4) if cons > 0.1 else None,
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
                FROM sma_live WHERE ts >= ? AND ts < ?
            """, (utc_od, utc_do)).fetchone()
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

        # Usporediv je samo ZAVRŠEN dan u kojem oba izvora imaju vrijednosti koje
        # se uspoređuju (HEP potrošnja/predaja ↔ SMA mreža/predaja). Današnji dan
        # je djelomičan (HEP kasni ~1 dan), a stariji SMA redovi imaju samo PV.
        for r in result:
            r['oba_izvora'] = bool(
                r['datum'] != today_str
                and all(r.get(k) is not None for k in (
                    'hep_potrosnja', 'hep_predaja', 'sma_mreza', 'sma_predaja')))

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
        if not _table_exists(conn, 'hep_registri'):
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


def _bijeli_mjeseci(conn):
    """Mjesečne bijeli (net-metering) metrike iz delta registara (A+/A-).

    Vraća listu dictova kronološki. Svaki: uzeto/predano VT+NT, mjesečni račun
    (pozitivni saldo), otkup viškova i korist PV-a. Korist = netting ušteda
    (predano smanjuje račun) + otkup viškova — KONZERVATIVNO jer registri ne
    znaju samopotrošnju (izbjegnutu kupnju iz vlastite proizvodnje), pa je ovo
    donja granica stvarne koristi.
    """
    if not _table_exists(conn, 'hep_registri'):
        return []
    rows = conn.execute(
        "SELECT datum, obis, value FROM hep_registri "
        "WHERE obis IN ('A+_T1','A+_T2','A-_T1','A-_T2') ORDER BY datum"
    ).fetchall()
    stanja = {}
    for r in rows:
        stanja.setdefault(r['datum'], {})[r['obis']] = r['value']
    datumi = sorted(stanja.keys())
    out = []
    for prev, cur in zip(datumi, datumi[1:]):
        s0, s1 = stanja[prev], stanja[cur]

        def d(k):
            a, b = s0.get(k), s1.get(k)
            return (b - a) if (a is not None and b is not None and b >= a) else None

        vt_p, nt_p = d('A+_T1'), d('A+_T2')     # uzeto iz mreže (VT/NT)
        if vt_p is None or nt_p is None:
            continue
        vt_pr = d('A-_T1') or 0                  # predano u mrežu (VT/NT)
        nt_pr = d('A-_T2') or 0
        try:
            n_dana = (date(*map(int, cur.split('-'))) -
                      date(*map(int, prev.split('-')))).days or 30
        except Exception:
            n_dana = 30
        # HEP fiksne naknade naplaćuje po mjesecu (ne po danu): puni mjesec = 30
        dana_obr = 30 if 28 <= n_dana <= 31 else n_dana
        # povijesni mjesec -> cjenik koji je tada vrijedio
        racun = izracunaj_racun_bijeli(vt_p, nt_p, vt_pr, nt_pr, dana_obr, datum=cur)
        otkup = otkup_viskova_bijeli(vt_p, nt_p, vt_pr, nt_pr, datum=cur)
        bez_predaje = izracunaj_racun_bijeli(vt_p, nt_p, 0, 0, dana_obr, datum=cur)['iznos']
        netting = round(bez_predaje - racun['iznos'], 2)
        out.append({
            'mjesec': cur[:7], 'od': prev, 'do': cur, 'n_dana': n_dana,
            'dana_obr': dana_obr,
            'uzeto_vt': round(vt_p, 2), 'uzeto_nt': round(nt_p, 2),
            'pred_vt': round(vt_pr, 2), 'pred_nt': round(nt_pr, 2),
            'uzeto_kwh': round(vt_p + nt_p, 2),
            'pred_kwh': round(vt_pr + nt_pr, 2),
            'racun_eur': racun['iznos'],
            'otkup_eur': otkup['ukupno_eur'],
            'netting_eur': netting,
            'korist_eur': round(netting + otkup['ukupno_eur'], 2),
        })
    return out


@bp.route('/godisnji-saldo')
def api_godisnji_saldo():
    """Godišnji (rolling 12 mj) net-metering saldo za bijeli model."""
    conn = get_db()
    try:
        svi = _bijeli_mjeseci(conn)
        zadnjih12 = svi[-12:]
        if not zadnjih12:
            return jsonify({'mjeseci': [], 'total': None})

        def s(k):
            return round(sum(m[k] for m in zadnjih12), 2)

        uzeto, pred = s('uzeto_kwh'), s('pred_kwh')
        placeno, otkup = s('racun_eur'), s('otkup_eur')
        total = {
            'od': zadnjih12[0]['od'], 'do': zadnjih12[-1]['do'],
            'n_mjeseci': len(zadnjih12),
            'uzeto_kwh': uzeto, 'pred_kwh': pred,
            'neto_saldo_kwh': round(pred - uzeto, 2),
            'uzeto_vt': s('uzeto_vt'), 'uzeto_nt': s('uzeto_nt'),
            'pred_vt': s('pred_vt'), 'pred_nt': s('pred_nt'),
            'placeno_eur': placeno, 'otkup_eur': otkup,
            'neto_trosak_eur': round(placeno - otkup, 2),
            'pokrivenost_perc': round(100 * pred / uzeto, 1) if uzeto > 0 else None,
        }
        return jsonify({'mjeseci': list(reversed(zadnjih12)), 'total': total})
    finally:
        conn.close()


@bp.route('/roi')
def api_roi():
    """Povrat investicije PV sustava (konzervativno, iz registara).

    Investicija: config PV_INVESTMENT_EUR. Datum puštanja: PV_COMMISSION_DATE.
    Payback = investicija / godišnja korist (run-rate iz dosad izmjerenih mj.).
    """
    conn = get_db()
    try:
        try:
            invest = float(get_config('PV_INVESTMENT_EUR', '') or 0) or None
        except (ValueError, TypeError):
            invest = None
        commission = get_config('PV_COMMISSION_DATE', '') or None

        svi = _bijeli_mjeseci(conn)
        n_mj = len(svi)
        korist_ukupno = round(sum(m['korist_eur'] for m in svi), 2)
        god_korist = round(korist_ukupno / n_mj * 12, 2) if n_mj else None
        payback_god = (round(invest / god_korist, 1)
                       if (invest and god_korist and god_korist > 0) else None)
        pokriveno = round(100 * korist_ukupno / invest, 1) if invest else None

        breakeven = None
        if commission and payback_god:
            try:
                y, m, _ = map(int, commission.split('-'))
                nm = int(round(payback_god * 12))
                breakeven = f'{y + (m - 1 + nm) // 12:04d}-{(m - 1 + nm) % 12 + 1:02d}'
            except Exception:
                breakeven = None

        return jsonify({
            'investicija_eur': invest,
            'commission': commission,
            'n_mjeseci': n_mj,
            'korist_ukupno_eur': korist_ukupno,
            'godisnja_korist_eur': god_korist,
            'payback_godina': payback_god,
            'pokriveno_perc': pokriveno,
            'breakeven_mjesec': breakeven,
            'mjeseci': list(reversed(svi)),
        })
    finally:
        conn.close()


@bp.route('/dug')
def api_dug():
    """Dug prema HEP-u + sezonska projekcija isplate + cijene u oba smjera."""
    from ..tariff import get_vt_udio_registri
    conn = get_db()
    try:
        racuni = [dict(r) for r in conn.execute(
            "SELECT period, iznos, dug, datum_racuna, datum_dospijeca "
            "FROM racuni WHERE dug IS NOT NULL ORDER BY period"
        ).fetchall()]

        # kronološki po normaliziranom periodu (stari zapis 'MM/YYYY' se inače krivo sortira)
        racuni.sort(key=lambda r: _norm_period(r["period"]) or "")

        # Δ dug prema prethodnom računu (negativno = dug pada)
        for preth, r in zip([None] + racuni, racuni):
            r["delta"] = round(r["dug"] - preth["dug"], 2) if preth else None

        # trenutni dug = isti račun koji bira zadnji_racun_i_dug (i /sazetak)
        trenutni = racuni[-1] if racuni else None
        vrh = max(racuni, key=lambda r: r["dug"]) if racuni else None

        pace = None
        if len(racuni) >= 2:
            zadnji = racuni[-4:]
            delte = [b["dug"] - a["dug"] for a, b in zip(zadnji, zadnji[1:])]
            if delte:
                pace = round(sum(delte) / len(delte), 2)

        reg_udjeli, reg_prosjek = get_vt_udio_registri(conn)

        po_kal = {}
        sezonski_izvor = "standardni"
        if is_bijeli(conn) and _table_exists(conn, 'hep_registri'):
            # Tocan bijeli net-metering racun iz delta registara (stanja brojila)
            reg = conn.execute(
                "SELECT datum, obis, value FROM hep_registri "
                "WHERE obis IN ('A+_T1','A+_T2','A-_T1','A-_T2') ORDER BY datum"
            ).fetchall()
            stanja = {}
            for rr in reg:
                stanja.setdefault(rr["datum"], {})[rr["obis"]] = rr["value"]
            datumi = sorted(stanja.keys())
            for prev, cur in zip(datumi, datumi[1:]):
                s0, s1 = stanja[prev], stanja[cur]
                def d(k):
                    a, b = s0.get(k), s1.get(k)
                    return (b - a) if (a is not None and b is not None and b >= a) else None
                vt_p, nt_p = d("A+_T1"), d("A+_T2")
                vt_pr, nt_pr = d("A-_T1"), d("A-_T2")
                if vt_p is None or nt_p is None:
                    continue
                # Namjerno trenutni cjenik (bez datum=): ovo je projekcija BUDUĆIH
                # računa iz povijesne potrošnje, ne rekonstrukcija starih.
                racun = izracunaj_racun_bijeli(vt_p, nt_p, vt_pr or 0, nt_pr or 0, 30)
                mes = int(cur[5:7])
                po_kal.setdefault(mes, []).append(racun["iznos"])
            if po_kal:
                sezonski_izvor = "bijeli (registri)"

        if not po_kal:
            # Fallback: standardni model iz dnevnih ocitanja
            rows = conn.execute(
                "SELECT substr(datum,1,7) mj, ROUND(SUM(kwh_plus),2) kp, "
                "ROUND(SUM(kwh_minus),2) km, COUNT(*) n "
                f"FROM ocitanja_dnevna WHERE datum >= {sql_today_local('-24 months')} "
                "GROUP BY substr(datum,1,7)"
            ).fetchall()
            for r in rows:
                if (r["n"] or 0) < 20:
                    continue
                mes = int(r["mj"][5:7])
                vt_u = reg_udjeli.get(r["mj"]) or reg_prosjek
                cost = izracunaj_racun(r["kp"] or 0, r["km"] or 0, r["n"], vt_udio=vt_u)
                po_kal.setdefault(mes, []).append(cost)

        sezonski = {m: round(sum(v) / len(v), 2) for m, v in po_kal.items()}
        godisnji_neto = round(sum(sezonski.values()), 2) if len(sezonski) == 12 else None

        t = get_tarifa()
        pdv = 1 + t["pdv"]
        vt_buy = (t["vt_opskrba"] + t["vt_distrib"] + t["vt_prijenos"] + t["solidarna"] + t["oie"]) * pdv
        nt_buy = (t["nt_opskrba"] + t["nt_distrib"] + t["nt_prijenos"] + t["solidarna"] + t["oie"]) * pdv
        vt_udio = reg_prosjek if reg_prosjek is not None else 0.45
        cijene = {
            "vt_kupnja":  round(vt_buy, 4),
            "nt_kupnja":  round(nt_buy, 4),
            "avg_kupnja": round(vt_buy * vt_udio + nt_buy * (1 - vt_udio), 4),
            "vt_otkup":   t["vt_otkup"],
            "nt_otkup":   t["nt_otkup"],
            "otkup":      t["otkup"],
            "pretplata":  round(t["opskrbna_mj"] + t["mjerna_mj"], 3),
            "vt_udio":    round(vt_udio * 100, 1),
        }

        return jsonify({
            "racuni":        racuni,
            "trenutni":      trenutni,
            "vrh":           vrh,
            "pace":          pace,
            "sezonski":      sezonski,
            "godisnji_neto": godisnji_neto,
            "sezonski_izvor": sezonski_izvor,
            "cijene":        cijene,
        })
    finally:
        conn.close()


def _autarkija_po_satu(conn, has_15min, has_sma):
    """Autarkija po satu dana u % (0–100) iz ZAJEDNIČKOG razdoblja HEP i SMA.

    Formula (prosjeci po satu dana, kWh/h):
        potrošnja kuće = uzeto iz mreže (HEP A+) + proizvodnja (SMA) − predano (HEP A−)
        autarkija      = 1 − uzeto iz mreže / potrošnja kuće,  ograničeno na 0..1
    Oba niza se računaju samo za dane u kojima postoje i HEP satni i SMA podaci
    (inače bi se miješale različite sezone). Vraća ({sat: % | None}, razdoblje | None).
    """
    sma_tab, pv_kol = (('sma_15min', 'pv_w_total') if has_15min else
                       ('sma_live', 'pv_generation_w') if has_sma else (None, None))
    if not sma_tab:
        return {}, None
    hep = conn.execute(f'''
        SELECT MIN(ts) as od, MAX(ts) as do FROM ocitanja_satna
        WHERE ts <= {SQL_NOW_LOCAL} AND (kwh_plus > 0 OR kwh_minus > 0)
    ''').fetchone()
    sma = conn.execute(
        f'SELECT MIN(ts) as od, MAX(ts) as do FROM {sma_tab} WHERE ts <= {SQL_NOW_UTC}'
    ).fetchone()
    sma_od, sma_do = _utc_u_lokalno(sma['od']), _utc_u_lokalno(sma['do'])
    if not (hep['od'] and hep['do'] and sma_od and sma_do):
        return {}, None
    od = max(hep['od'][:10], sma_od.strftime('%Y-%m-%d'))
    do = min(hep['do'][:10], sma_do.strftime('%Y-%m-%d'))
    if od > do:
        return {}, None

    uzeto = {r['sat']: r for r in conn.execute(f'''
        SELECT CAST(strftime('%H', ts) AS INTEGER) as sat,
               AVG(kwh_plus) as uzeto, AVG(kwh_minus) as predano
        FROM ocitanja_satna
        WHERE ts >= ? AND ts <= ? AND ts <= {SQL_NOW_LOCAL}
          AND (kwh_plus > 0 OR kwh_minus > 0)
        GROUP BY CAST(strftime('%H', ts) AS INTEGER)
    ''', (od + 'T00:00:00', do + 'T23:59:59')).fetchall()}
    # prosječna snaga u satu (kW) = kWh u tom satu; SMA ts je UTC -> lokalni sat
    pv = {r['sat']: r['pv'] or 0 for r in conn.execute(f'''
        SELECT CAST(strftime('%H', ts, 'localtime') AS INTEGER) as sat,
               AVG({pv_kol}) / 1000.0 as pv
        FROM {sma_tab}
        WHERE ts >= ? AND ts < ?
        GROUP BY CAST(strftime('%H', ts, 'localtime') AS INTEGER)
    ''', utc_raspon_dana(od, do)).fetchall()}

    out = {}
    for sat, r in uzeto.items():
        if sat not in pv:
            out[sat] = None
            continue
        u = r['uzeto'] or 0
        kuca = u + pv[sat] - (r['predano'] or 0)
        out[sat] = (round(100 * min(1.0, max(0.0, 1 - u / kuca)), 1)
                    if kuca > 0.001 else None)
    return out, {'od': od, 'do': do}


@bp.route('/optimalno')
def api_optimalno():
    conn = get_db()
    try:
        has_sma = _table_exists(conn, 'sma_live')

        # "red ima podatke" = potrošnja ILI predaja: sunčani sati u kojima kuća
        # samo predaje (kwh_plus = 0) moraju ući u prosjek.
        hep_satno = conn.execute(f'''
            SELECT CAST(strftime('%H', ts) AS INTEGER) as sat,
                   ROUND(AVG(kwh_plus), 4) as prosj_potrosnja,
                   ROUND(AVG(kwh_minus), 4) as prosj_predaja,
                   COUNT(*) as n
            FROM ocitanja_satna
            WHERE ts <= {SQL_NOW_LOCAL} AND (kwh_plus > 0 OR kwh_minus > 0)
            GROUP BY CAST(strftime('%H', ts) AS INTEGER)
            ORDER BY sat
        ''').fetchall()

        sma_satno = []
        has_15min = _table_exists(conn, 'sma_15min')
        # SMA ts je UTC -> sat dana se računa lokalno ('localtime'), kao HEP sat
        if has_15min:
            sma_satno = conn.execute('''
                SELECT CAST(strftime('%H', ts, 'localtime') AS INTEGER) as sat,
                       ROUND(AVG(pv_w_total) / 1000.0, 3) as prosj_pv_w,
                       ROUND(AVG(pv_w_inv1) / 1000.0, 3) as prosj_inv1_kw,
                       ROUND(AVG(pv_w_inv2) / 1000.0, 3) as prosj_inv2_kw,
                       COUNT(*) as n
                FROM sma_15min
                GROUP BY CAST(strftime('%H', ts, 'localtime') AS INTEGER)
                ORDER BY sat
            ''').fetchall()
        elif has_sma:
            sma_satno = conn.execute(f'''
                SELECT CAST(strftime('%H', ts, 'localtime') AS INTEGER) as sat,
                       ROUND(AVG(pv_generation_w) / 1000.0, 3) as prosj_pv_w,
                       0 as prosj_inv1_kw,
                       0 as prosj_inv2_kw,
                       COUNT(*) as n
                FROM sma_live
                WHERE ts <= {SQL_NOW_UTC}
                GROUP BY CAST(strftime('%H', ts, 'localtime') AS INTEGER)
                ORDER BY sat
            ''').fetchall()

        hep_out = [dict(r) for r in hep_satno]
        try:
            aut, razdoblje = _autarkija_po_satu(conn, has_15min, has_sma)
        except Exception:
            log.exception('optimalno: autarkija po satu nije uspjela')
            aut, razdoblje = {}, None
        for r in hep_out:
            r['autarkija'] = aut.get(r['sat'])

        return jsonify({
            'hep_satno': hep_out,
            'sma_satno': [dict(r) for r in sma_satno],
            'autarkija_razdoblje': razdoblje,
        })
    finally:
        conn.close()


def _vt_sati():
    """(vt_od, vt_do) — zimski VT sati iz configa (vidi timeutil.vt_sql)."""
    try:
        return int(get_config('TARIFA_VT_OD', '7')), int(get_config('TARIFA_VT_DO', '21'))
    except (TypeError, ValueError):
        return 7, 21


def _mjeseci_s_procjenom(conn, procjena=None):
    """Mjesečni pregled (zadnjih 36 mj, najnoviji prvi) s procjenom računa po
    STVARNOM modelu kupca. Vraća (mjeseci, racuni, bijeli).

    Dijele ga /mjesecni i /sazetak da oba prikazuju iste brojke. Uz stara polja
    (procj_racun = standardna formula, ne mijenjati) svaki mjesec dobiva:
      procj_iznos  — procjena računa u €, POZITIVNO = za platiti
      procj_izvor  — 'registri'   bijeli iz delta registara brojila (točno ±0,15 €)
                     'projekcija' tekući mjesec, ista logika kao /procjena-trenutni
                     'satna'      bijeli iz satnih očitanja (mjesec bez registara)
                     'standardni' standardna formula (zadnji izlaz)
      otkup_eur    — otkup viškova (bijeli; inače 0)
      stvarni_iznos / razlika — uvezeni račun i (procj_iznos − stvarni_iznos)
    """
    from ..tariff import get_vt_udio_registri
    bijeli = is_bijeli(conn)
    model = 'bijeli' if bijeli else 'standardni'
    tekuci = today_local()[:7]

    rows = []
    if _table_exists(conn, 'ocitanja_dnevna'):
        rows = conn.execute(f'''
            SELECT substr(h.datum,1,7) as mjesec,
                   ROUND(SUM(h.kwh_plus),2)  as hep_potrosnja,
                   ROUND(SUM(h.kwh_minus),2) as hep_predaja,
                   COUNT(h.datum) as n_dana
            FROM ocitanja_dnevna h
            WHERE h.datum >= {sql_today_local('-36 months')}
            GROUP BY substr(h.datum,1,7)
            ORDER BY mjesec DESC
        ''').fetchall()

    reg_udjeli, reg_prosjek = get_vt_udio_registri(conn)

    # SMA agregati za sve mjesece odjednom (umjesto upita po mjesecu)
    has_sma = _table_exists(conn, 'sma_dnevna')
    sma_po_mj = {}
    if has_sma:
        for s in conn.execute('''
            SELECT substr(datum,1,7) as mjesec,
                   ROUND(SUM(pv_generation_kwh),2) as pv,
                   ROUND(AVG(autarky_rate)*100,1) as autarkija
            FROM sma_dnevna
            GROUP BY substr(datum,1,7)
        ''').fetchall():
            sma_po_mj[s['mjesec']] = s

    racuni = []
    if _table_exists(conn, 'racuni'):
        racuni = [dict(r) for r in conn.execute(
            'SELECT * FROM racuni ORDER BY period DESC'
        ).fetchall()]
    racun_po_mj = {}
    for rc in racuni:
        racun_po_mj.setdefault(_norm_period(rc.get('period')), rc)

    # Bijeli: službene delte registara; samo puni obračunski mjeseci (28–31 dan)
    reg_po_mj = {}
    if bijeli:
        reg_po_mj = {m['mjesec']: m for m in _bijeli_mjeseci(conn)
                     if 28 <= m['n_dana'] <= 31}
    satna_po_mj = None   # lijeno: treba samo bijelim mjesecima bez registara

    def _satna(mj):
        nonlocal satna_po_mj
        if satna_po_mj is None:
            satna_po_mj = {}
            if _table_exists(conn, 'ocitanja_satna'):
                je_vt = vt_sql('ts', *_vt_sati())
                for s in conn.execute(f'''
                    SELECT substr(ts,1,7) as mj,
                           SUM(CASE WHEN {je_vt} THEN kwh_plus  ELSE 0 END) as vt,
                           SUM(CASE WHEN {je_vt} THEN 0 ELSE kwh_plus  END) as nt,
                           SUM(CASE WHEN {je_vt} THEN kwh_minus ELSE 0 END) as vt_pred,
                           SUM(CASE WHEN {je_vt} THEN 0 ELSE kwh_minus END) as nt_pred,
                           COUNT(DISTINCT substr(ts,1,10)) as n
                    FROM ocitanja_satna
                    WHERE ts >= {sql_today_local('-36 months')}
                    GROUP BY substr(ts,1,7)
                ''').fetchall():
                    satna_po_mj[s['mj']] = s
        return satna_po_mj.get(mj)

    result = []
    for row in rows:
        r = dict(row)
        mj = r['mjesec']
        kp = r['hep_potrosnja'] or 0
        km = r['hep_predaja'] or 0
        n  = r['n_dana'] or 1
        # sluzbeni VT udio za taj mjesec iz registara; fallback slider/prosjek
        vt_u = reg_udjeli.get(mj) or reg_prosjek
        r['vt_udio_registri'] = round(vt_u * 100, 1) if vt_u is not None else None
        r['procj_racun'] = izracunaj_racun(kp, km, n, vt_udio=vt_u, datum=mj)
        vt_eff = vt_u if vt_u is not None else 0.45
        t_mj = get_tarifa(mj)
        r['procj_trosak_neto'] = round(
            kp * (t_mj['vt_opskrba'] * vt_eff + t_mj['nt_opskrba'] * (1 - vt_eff)) -
            km * t_mj['otkup'], 2)
        r['otkup_cijena'] = t_mj['otkup']   # cijena otkupa koja je vrijedila taj mjesec
        if has_sma:
            sma = sma_po_mj.get(mj)
            r['sma_pv']        = sma['pv'] if sma else None
            r['sma_autarkija'] = sma['autarkija'] if sma else None

        # --- procjena po stvarnom modelu (pozitivno = za platiti) ---
        reg = reg_po_mj.get(mj)
        sat = None
        if reg:
            iznos, otkup, izvor = reg['racun_eur'], reg['otkup_eur'], 'registri'
        elif mj == tekuci and procjena and procjena['ima_podataka']:
            iznos = procjena['racun_procjena']
            otkup = (procjena['otkup_procjena'] or {}).get('ukupno_eur', 0)
            izvor = 'projekcija'
        elif bijeli and (sat := _satna(mj)):
            # fiksne naknade idu po mjesecu (kao u _bijeli_mjeseci)
            dana = 30 if sat['n'] >= 28 else sat['n']
            a = (sat['vt'] or 0, sat['nt'] or 0, sat['vt_pred'] or 0, sat['nt_pred'] or 0)
            iznos = izracunaj_racun_bijeli(*a, dana, datum=mj)['iznos']
            otkup = otkup_viskova_bijeli(*a, datum=mj)['ukupno_eur']
            izvor = 'satna'
        else:
            iznos, otkup, izvor = r['procj_racun'], 0, 'standardni'
        rc = racun_po_mj.get(mj)
        stvarni = rc['iznos'] if rc else None
        r['model'] = model
        r['procj_iznos'] = round(iznos, 2)
        r['procj_izvor'] = izvor
        r['otkup_eur'] = round(otkup or 0, 2)
        r['stvarni_iznos'] = stvarni
        r['razlika'] = round(iznos - stvarni, 2) if stvarni is not None else None
        result.append(r)

    return result, racuni, bijeli


def _zbroj_mjeseci(mjeseci):
    """Računi/otkup/neto preko zadanih mjeseci — stvarni račun ako je uvezen,
    inače procjena. Vraća (sažetak, [(mjesec, iznos)])."""
    iznosi = [(m['mjesec'], m['stvarni_iznos'] if m['stvarni_iznos'] is not None
               else m['procj_iznos']) for m in mjeseci]
    rac = round(sum(i for _, i in iznosi), 2)
    otk = round(sum(m['otkup_eur'] for m in mjeseci), 2)
    return {
        'racuni_eur': rac, 'otkup_eur': otk, 'neto_eur': round(rac - otk, 2),
        'n_mjeseci': len(mjeseci),
    }, iznosi


def _zadnjih_12(mjeseci):
    """Zadnjih 12 ZAVRŠENIH mjeseci (tekući se ne broji) + najskuplji među njima."""
    tekuci = today_local()[:7]
    zatvoreni = [m for m in mjeseci if m['mjesec'] < tekuci][:12]
    saz, iznosi = _zbroj_mjeseci(zatvoreni)
    naj = max(iznosi, key=lambda x: x[1]) if iznosi else None
    saz['najskuplji'] = {'mjesec': naj[0], 'iznos': round(naj[1], 2)} if naj else None
    return saz


@bp.route('/mjesecni')
def api_mjesecni():
    conn = get_db()
    try:
        result, racuni, bijeli = _mjeseci_s_procjenom(conn, _procjena_trenutni(conn))
        return jsonify({
            'mjeseci': result,
            'tarifa':  dict(get_tarifa()),
            'racuni':  racuni,
            'model':   'bijeli' if bijeli else 'standardni',
            'sazetak_12mj': _zadnjih_12(result),
        })
    finally:
        conn.close()


def _procjena_trenutni(conn):
    """Procjena računa za TEKUĆI mjesec na temelju dosadašnje potrošnje (dict).

    Logika:
      - Sumiraj potrošnju/predaju od 1. do danas iz ocitanja_satna
      - Računaj prorate na cijeli mjesec (× n_dana_mjesec / proteklo_dana)
      - Primijeni HEP tarife + VT/NT slider + pretplate
      - Vrati: proteklo, projicirano za mjesec, postotak proteklog mjeseca

    Jedini izvor brojki tekućeg mjeseca: /procjena-trenutni, /mjesecni
    (procj_izvor='projekcija') i /sazetak zovu ovu funkciju. Ne baca iznimku
    na praznoj bazi (nema tablice/redova → nule, ima_podataka=False).
    """
    from calendar import monthrange
    from ..tariff import get_vt_udio, get_vt_udio_registri

    now = now_local()
    god, mj = now.year, now.month
    n_dana_mj = monthrange(god, mj)[1]
    proteklo_dana = now.day  # uključno do današnjeg dana

    # VT po UTC satu (zimi 07-21, ljeti 08-22 lokalno) — vidi timeutil.vt_sql
    je_vt = vt_sql('ts', *_vt_sati())

    mj_str = f'{god:04d}-{mj:02d}'
    mj_od, mj_do = raspon_mjeseca(mj_str)
    row = {}
    if _table_exists(conn, 'ocitanja_satna'):
        row = dict(conn.execute(f'''
            SELECT
                ROUND(SUM(kwh_plus), 2) as kwh_plus,
                ROUND(SUM(kwh_minus), 2) as kwh_minus,
                ROUND(SUM(CASE WHEN {je_vt} THEN kwh_plus ELSE 0 END), 2) as kwh_vt,
                ROUND(SUM(CASE WHEN {je_vt} THEN 0 ELSE kwh_plus END), 2) as kwh_nt,
                ROUND(SUM(CASE WHEN {je_vt} THEN kwh_minus ELSE 0 END), 2) as kwh_vt_pred,
                ROUND(SUM(CASE WHEN {je_vt} THEN 0 ELSE kwh_minus END), 2) as kwh_nt_pred,
                MAX(ts) as zadnji_ts,
                MIN(ts) as prvi_ts
            FROM ocitanja_satna
            WHERE ts >= ? AND ts < ?
        ''', (mj_od, mj_do)).fetchone())

    # zadnji uvezeni račun za pretplatu
    last_bill = None
    if _table_exists(conn, 'racuni'):
        last_bill = conn.execute('''
            SELECT pretplata, mjerna_mjernina FROM racuni
            WHERE pretplata IS NOT NULL OR model_tarife IS NOT NULL
            ORDER BY datum_racuna DESC LIMIT 1
        ''').fetchone()

    # Stvarni VT udio za sad (registri fallback ide istom konekcijom)
    if (row.get('kwh_plus') or 0) > 0:
        vt_udio_real = (row.get('kwh_vt') or 0) / row['kwh_plus']
    else:
        # nema satnih podataka ovaj mjesec — probaj sluzbeni prosjek iz registara
        _, reg_prosjek = get_vt_udio_registri(conn)
        vt_udio_real = reg_prosjek if reg_prosjek is not None else get_vt_udio()

    bijeli = is_bijeli(conn)

    kp = row.get('kwh_plus') or 0
    km = row.get('kwh_minus') or 0
    kvt = row.get('kwh_vt') or 0
    knt = row.get('kwh_nt') or 0
    kvt_pred = row.get('kwh_vt_pred') or 0
    knt_pred = row.get('kwh_nt_pred') or 0
    # broj dana s podacima (dani s ts u tekućem mjesecu)
    prvi, zadnji = row.get('prvi_ts'), row.get('zadnji_ts')
    ima_podataka = bool(prvi and zadnji)
    try:
        if ima_podataka:
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

    proj_kvt = round(kvt * faktor, 2)
    proj_knt = round(knt * faktor, 2)
    proj_kvt_pred = round(kvt_pred * faktor, 2)
    proj_knt_pred = round(knt_pred * faktor, 2)

    if bijeli:
        # HEPI bijeli — net metering
        det_proteklo = izracunaj_racun_bijeli(kvt, knt, kvt_pred, knt_pred, dani_s_podacima)
        det_proj     = izracunaj_racun_bijeli(proj_kvt, proj_knt, proj_kvt_pred, proj_knt_pred, n_dana_mj)
        otkup_proteklo = otkup_viskova_bijeli(kvt, knt, kvt_pred, knt_pred)
        otkup_proj     = otkup_viskova_bijeli(proj_kvt, proj_knt, proj_kvt_pred, proj_knt_pred)
        proteklo_racun = det_proteklo['iznos']
        proj_racun     = det_proj['iznos']
    else:
        proteklo_racun = izracunaj_racun(kp, km, dani_s_podacima, vt_udio=vt_udio_real)
        proj_racun     = izracunaj_racun(proj_kp, proj_km, n_dana_mj, vt_udio=vt_udio_real)
        det_proteklo = det_proj = None
        otkup_proteklo = otkup_proj = None
    model_kod = 'bijeli' if bijeli else 'standardni'

    # Pretplate (fiksni dio)
    t = get_tarifa()
    pret_opskrba = (last_bill['pretplata'] if last_bill else None) or t['opskrbna_mj']
    pret_mjerna  = (last_bill['mjerna_mjernina'] if last_bill else None) or t['mjerna_mj']
    fiksno = round(pret_opskrba + pret_mjerna, 2)

    return {
        'mjesec':              mj_str,
        'model':               MODEL_NAZIV[model_kod],
        'model_kod':           model_kod,
        'ima_podataka':        ima_podataka,
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
            'vt_kwh': round(t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos'], 6),
            'nt_kwh': round(t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos'], 6),
            'pretplata':  fiksno,
            'pdv':        t['pdv'],
            'otkup':      t['otkup'],
            'vt_otkup':   t['vt_otkup'],
            'nt_otkup':   t['nt_otkup'],
        },
    }


@bp.route('/procjena-trenutni')
def api_procjena_trenutni():
    """Procjena računa za tekući mjesec — vidi `_procjena_trenutni`."""
    conn = get_db()
    try:
        return jsonify(_procjena_trenutni(conn))
    finally:
        conn.close()


def _bez_solara(conn, mjeseci, bijeli):
    """Koliko bi završeni mjeseci (lista iz _mjeseci_s_procjenom) koštali BEZ
    elektrane — procjena.

    Po mjesecu: samopotrošnja = SMA proizvodnja − predano u mrežu (≥ 0); račun
    se ponovno izračuna s potrošnjom = uzeto iz mreže + samopotrošnja (sva u VT:
    sunce radi u VT satima) i predajom 0. Preskaču se mjeseci bez PV podataka.
    Vraća None ako nijedan mjesec nije izračunljiv.
    """
    reg_po_mj = {}
    if bijeli:
        reg_po_mj = {m['mjesec']: m for m in _bijeli_mjeseci(conn)
                     if 28 <= m['n_dana'] <= 31}
    bez = stvarno = 0.0
    n = 0
    for m in mjeseci:
        pv = m.get('sma_pv')
        if not pv:
            continue
        reg = reg_po_mj.get(m['mjesec'])
        if reg:
            vlastito = max(0.0, pv - reg['pred_kwh'])
            iznos = izracunaj_racun_bijeli(
                reg['uzeto_vt'] + vlastito, reg['uzeto_nt'], 0, 0,
                reg['dana_obr'], datum=reg['do'])['iznos']
        else:
            kp = m['hep_potrosnja'] or 0
            vlastito = max(0.0, pv - (m['hep_predaja'] or 0))
            vt_u = (m['vt_udio_registri'] or 45.0) / 100.0
            if bijeli:
                iznos = izracunaj_racun_bijeli(
                    kp * vt_u + vlastito, kp * (1 - vt_u), 0, 0,
                    30 if m['n_dana'] >= 28 else m['n_dana'], datum=m['mjesec'])['iznos']
            else:
                uk = kp + vlastito
                iznos = izracunaj_racun(
                    uk, 0, m['n_dana'],
                    vt_udio=(kp * vt_u + vlastito) / uk if uk > 0 else vt_u,
                    datum=m['mjesec'])
        placeno = m['stvarni_iznos'] if m['stvarni_iznos'] is not None else m['procj_iznos']
        bez += iznos
        stvarno += placeno - m['otkup_eur']
        n += 1
    if not n:
        return None
    return {
        'godina_eur': round(bez, 2),
        'usteda_eur': round(bez - stvarno, 2),
        'neto_eur':   round(stvarno, 2),
        'n_mjeseci':  n,
        'napomena': (
            f'Procjena za {n} završenih mj. ove godine: račun ponovno izračunat kao da je '
            'vlastito potrošena sunčeva energija (SMA proizvodnja − predano u mrežu) '
            'kupljena od HEP-a po višoj tarifi, bez predaje u mrežu. Ušteda = taj iznos '
            '− (stvarni računi − otkup). Tekući mjesec nije uključen.'),
    }


@bp.route('/sazetak')
def api_sazetak():
    """Sažetak za naslovnicu i Financije — model, zadnji račun/dug, tekući
    mjesec, godina do danas, zadnjih 12 mj i procjena "bez solara".

    Iznosi: račun/trošak POZITIVAN (za platiti), otkup pozitivan (prihod),
    neto = računi − otkup. Brojke tekućeg mjeseca su iz `_procjena_trenutni`,
    mjesečne iz `_mjeseci_s_procjenom` (isto što vraća /mjesecni).
    """
    conn = get_db()
    try:
        p = _procjena_trenutni(conn)
        mjeseci, racuni, bijeli = _mjeseci_s_procjenom(conn, p)
        model = 'bijeli' if bijeli else 'standardni'
        danas = today_local()
        tekuci, god = danas[:7], danas[:4]

        # Zadnji uvezeni račun + dug. Dug i dospijeće idu s najnovijeg računa
        # koji IMA dug (isti koji /dug vraća kao 'trenutni'): ručno uneseni
        # račun nema ni dug ni dospijeće pa ne smije sakriti poznati dug.
        # 'dug_period' kaže s kojeg je računa dug (None = dug nepoznat).
        racun = None
        rc, rd = zadnji_racun_i_dug(conn)
        if rc:
            izv = rd or rc
            dosp = izv.get('datum_dospijeca')
            try:
                do_dosp = (date.fromisoformat(dosp[:10]) - date.fromisoformat(danas)).days
            except (TypeError, ValueError):
                do_dosp = None
            racun = {
                'period': _norm_period(rc.get('period')) or rc.get('period'),
                'iznos': rc.get('iznos'),
                'dug': izv.get('dug'),
                'dug_period': (_norm_period(rd.get('period')) or rd.get('period')) if rd else None,
                'datum_dospijeca': dosp or None,
                'dana_do_dospijeca': do_dosp,
            }

        otkup_proc = (p['otkup_procjena'] or {}).get('ukupno_eur', 0) or 0
        mjesec = {
            'mjesec':             p['mjesec'],
            'ima_podataka':       p['ima_podataka'],
            'dani_s_podacima':    p['dani_s_podacima'],
            'n_dana_mjesec':      p['n_dana_mjesec'],
            'do_sada_eur':        p['racun_proteklo'],
            'procjena_eur':       p['racun_procjena'],
            'otkup_procjena_eur': otkup_proc,
            'neto_procjena_eur':  round(p['racun_procjena'] - otkup_proc, 2),
            'vt_udio':            p['vt_udio_stvarni'],
        }

        # Godina do danas: završeni mjeseci + (ako ima podataka) projekcija tekućeg
        zatvoreni = [m for m in mjeseci if m['mjesec'][:4] == god and m['mjesec'] < tekuci]
        godina, _ = _zbroj_mjeseci(zatvoreni)
        if p['ima_podataka']:
            godina['racuni_eur'] = round(godina['racuni_eur'] + p['racun_procjena'], 2)
            godina['otkup_eur']  = round(godina['otkup_eur'] + otkup_proc, 2)
            godina['neto_eur']   = round(godina['racuni_eur'] - godina['otkup_eur'], 2)
            godina['n_mjeseci'] += 1
        godina = {'godina': int(god), **godina,
                  'ukljucuje_procjenu_tekuceg': p['ima_podataka']}

        z12 = _zadnjih_12(mjeseci)
        z12.pop('najskuplji', None)

        return jsonify({
            'model':        model,
            'model_naziv':  MODEL_NAZIV[model],
            'cijene_izvor': get_tarifa_izvor(),
            'racun':        racun,
            'mjesec':       mjesec,
            'godina':       godina,
            'zadnjih_12':   z12,
            'bez_solara':   _bez_solara(conn, zatvoreni, bijeli),
        })
    finally:
        conn.close()


def _utc_u_lokalno(ts):
    """UTC zapis iz baze ('...+00:00', '...Z' ili 'YYYY-MM-DD HH:MM:SS') →
    aware lokalni datetime; None ako se ne da pročitati."""
    try:
        d = datetime.fromisoformat(str(ts).strip().replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(TZ)


@bp.route('/status')
def api_status():
    """Svježina podataka za zaglavlje (HEP kasni ~1 dan, SMA je uživo).

    Samo jeftini upiti (zadnji red po indeksu); svaki dio je zaseban pa greška
    jednog izvora ne ruši ostale. Vremena u odgovoru su LOKALNA, 'T' format.
    """
    fmt = '%Y-%m-%dT%H:%M:%S'
    sada = now_local()
    hep  = {'zadnji_uzorak': None, 'zadnji_dan': None, 'kasni_h': None, 'ok': False}
    sma  = {'zadnji': None, 'kasni_min': None, 'ok': False}
    sync = {'zadnji_ok': None, 'greske_24h': 0}
    model = 'standardni'
    conn = get_db()
    try:
        try:
            # zadnji STVARNI 15-min uzorak (scraper zna upisati i prazne redove);
            # ORDER BY ts DESC ide po indeksu i staje na prvom pogotku
            tablica = next((t for t in ('ocitanja_15min', 'ocitanja_satna')
                            if _table_exists(conn, t)), None)
            r = conn.execute(f'''
                SELECT ts FROM {tablica}
                WHERE ts <= {SQL_NOW_LOCAL} AND (kwh_plus > 0 OR kwh_minus > 0)
                ORDER BY ts DESC LIMIT 1
            ''').fetchone() if tablica else None
            if r and r['ts']:
                uzorak = datetime.fromisoformat(r['ts'])
                kasni = (sada.replace(tzinfo=None) - uzorak).total_seconds() / 3600.0
                hep = {'zadnji_uzorak': r['ts'], 'kasni_h': round(max(kasni, 0), 1),
                       'ok': kasni < 48}
                if tablica == 'ocitanja_15min':
                    # uzorak nosi oznaku KRAJA intervala: 00:00 = podaci do kraja jučerašnjeg dana
                    hep['zadnji_dan'] = (uzorak - timedelta(minutes=15)).strftime('%Y-%m-%d')
                else:
                    hep['zadnji_dan'] = r['ts'][:10]
        except Exception:
            log.exception('status: HEP dio nije uspio')
        try:
            if _table_exists(conn, 'sma_live'):
                # po id-u (monotono), ne po ts — pisači koriste različite TZ formate
                r = conn.execute('SELECT ts FROM sma_live ORDER BY id DESC LIMIT 1').fetchone()
                lok = _utc_u_lokalno(r['ts']) if r else None
                if lok:
                    kasni = int(max((sada - lok).total_seconds(), 0) // 60)
                    sma = {'zadnji': lok.strftime(fmt), 'kasni_min': kasni, 'ok': kasni < 15}
        except Exception:
            log.exception('status: SMA dio nije uspio')
        try:
            if _table_exists(conn, 'sync_log'):
                # sync_log.ts je UTC s razmakom -> granica istim formatom (datetime('now'))
                r = conn.execute(
                    "SELECT ts FROM sync_log WHERE status='OK' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                lok = _utc_u_lokalno(r['ts']) if r else None
                sync['zadnji_ok'] = lok.strftime(fmt) if lok else None
                sync['greske_24h'] = conn.execute(
                    "SELECT COUNT(*) FROM sync_log WHERE status='ERROR' "
                    "AND ts > datetime('now','-1 day')").fetchone()[0]
        except Exception:
            log.exception('status: sync dio nije uspio')
        if is_bijeli(conn):
            model = 'bijeli'
    finally:
        conn.close()
    try:
        izvor = get_tarifa_izvor()
    except Exception:
        izvor = None
    return jsonify({'hep': hep, 'sma': sma, 'sync': sync,
                    'cijene_izvor': izvor, 'model': model})


@bp.route('/cijene')
def api_cijene():
    """Stvarne aktivne cijene za prikaz korisniku.

    Izvori (po prioritetu):
      1. Zadnji uvezeni HEP račun (pretplata, mjerna_mjernina)
      2. Važeći cjenik — tariff.get_tarifa() (VT/NT/distrib/prijenos/solidarna/oie/PDV/otkup)
      3. VT_UDIO_PERC iz config-a
    """
    from .. import tariff
    from ..tariff import get_vt_udio, get_vt_udio_registri
    conn = get_db()
    try:
        row = conn.execute('''
            SELECT pretplata, mjerna_mjernina, datum_racuna, period, model_tarife
            FROM racuni
            WHERE pretplata IS NOT NULL OR mjerna_mjernina IS NOT NULL
            ORDER BY datum_racuna DESC, period DESC LIMIT 1
        ''').fetchone()
        _, reg_prosjek = get_vt_udio_registri(conn)
    finally:
        conn.close()

    t = get_tarifa()
    pretplata_opskrba = (row['pretplata'] if row else None) or t['opskrbna_mj']
    pretplata_mjerna  = (row['mjerna_mjernina'] if row else None) or t['mjerna_mj']
    vt_udio_izvor = 'registri (prosjek 3 mj)' if reg_prosjek is not None else 'slider'
    vt_udio = reg_prosjek if reg_prosjek is not None else get_vt_udio()

    # Puna cijena VT/NT po kWh (uključujući sve sastavnice + PDV)
    vt_full = (t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos']
               + t['solidarna'] + t['oie']) * (1 + t['pdv'])
    nt_full = (t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos']
               + t['solidarna'] + t['oie']) * (1 + t['pdv'])
    avg_full = vt_full * vt_udio + nt_full * (1 - vt_udio)

    # Izvor cijena: tariff.get_tarifa_izvor() ako postoji (cjenik izveden iz
    # uvezenih računa); inače stari opis (zadnji račun s pretplatom).
    izvor = None
    izvor_fn = getattr(tariff, 'get_tarifa_izvor', None)
    if callable(izvor_fn):
        try:
            izvor = izvor_fn()
        except Exception:
            log.exception('get_tarifa_izvor nije uspio')
    if not izvor:
        izvor = (row['period'] + ' (' + (row['model_tarife'] or '') + ')'
                 if row else 'default HEP_TARIFA')

    return jsonify({
        'izvor':              izvor,
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
        'vt_udio_izvor':      vt_udio_izvor,
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
    except Exception:
        log.exception('open-meteo dohvat nije uspio')
        return jsonify({'error': 'Vremenska prognoza trenutno nije dostupna'}), 502


@bp.route('/power-limit-history')
def api_power_limit_history():
    """24h history inverter power limit-a iz HA."""
    import os, requests
    from netutil import ha_verify

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
            timeout=15, verify=ha_verify(),
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
        # samo tip greške: poruka requests iznimke sadrži HA URL
        log.warning('HA power-limit history nije uspio: %s', type(e).__name__)
        return jsonify({'series': [], 'note': 'HA trenutno nije dostupan'})


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
    import os, requests
    from netutil import ha_verify

    nominal_kw     = float(get_config('PV_NOMINAL_KW', '0') or 0)
    commission     = get_config('PV_COMMISSION_DATE', '')
    co2_factor     = float(get_config('PV_CO2_FACTOR_G_KWH', '640') or 640)
    feed_tariff    = float(get_config('PV_FEED_TARIFF_EUR', '') or get_tarifa().get('otkup', 0.064379))
    power_limit_ent = get_config('HA_ENT_POWER_LIMIT', '')

    today_str = today_local()
    mj_od, mj_do = raspon_mjeseca(today_str[:7])
    god = int(today_str[:4])

    conn = get_db()
    try:
        pv_today = conn.execute(
            "SELECT pv_generation_kwh FROM sma_dnevna WHERE datum=?", (today_str,)
        ).fetchone()
        pv_today = pv_today['pv_generation_kwh'] if pv_today and pv_today['pv_generation_kwh'] else 0

        pv_month = conn.execute(
            "SELECT ROUND(SUM(pv_generation_kwh),2) as s FROM sma_dnevna "
            "WHERE datum >= ? AND datum < ?",
            (mj_od, mj_do)
        ).fetchone()['s'] or 0

        pv_year = conn.execute(
            "SELECT ROUND(SUM(pv_generation_kwh),2) as s FROM sma_dnevna "
            "WHERE datum >= ? AND datum < ?",
            (f'{god:04d}-01-01', f'{god + 1:04d}-01-01')
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
                    timeout=8, verify=ha_verify(),
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
    # Razdoblja su ugniježđena (dan ⊂ tjedan ⊂ mjesec ⊂ godina ⊂ uvijek), pa se
    # svaki red svrsta u NAJUŽE razdoblje kojem pripada i po tom razredu uzme
    # MAX; peak šireg razdoblja = max preko svih užih razreda. Tako su dovoljna
    # 2 prolaza kroz tablicu (potrošnja, predaja) umjesto 10.
    # ts je lokalni 'T' format -> granice iz sql_now_local(), ne datetime('now').
    labele = ['dan', 'tjedan', 'mjesec', 'godina', 'uvijek']
    razred = f'''CASE WHEN ts >= {sql_now_local('-1 days')}   THEN 0
                     WHEN ts >= {sql_now_local('-7 days')}   THEN 1
                     WHEN ts >= {sql_now_local('-30 days')}  THEN 2
                     WHEN ts >= {sql_now_local('-365 days')} THEN 3
                     ELSE 4 END'''

    def _peak(kol, uvjet=''):
        # SQLite: uz jedan MAX() "gola" kolona ts dolazi iz reda s maksimumom
        rows = conn.execute(f'''
            SELECT {razred} as razred, ts,
                   MAX({kol}) as kwh, ROUND(MAX({kol}) * 4, 2) as kw
            FROM ocitanja_15min
            WHERE ts <= {SQL_NOW_LOCAL} {uvjet}
            GROUP BY razred
        ''').fetchall()
        po_razredu = {r['razred']: r for r in rows}
        res, best = [], None
        for i in range(len(labele)):
            r = po_razredu.get(i)
            if r is not None and (best is None or (
                    r['kwh'] is not None
                    and (best['kwh'] is None or r['kwh'] > best['kwh']))):
                best = r
            res.append(best)
        return res

    conn = get_db()
    try:
        potr = _peak('kwh_plus')
        pred = _peak('kwh_minus', 'AND kwh_minus > 0')
        out = {}
        for i, label in enumerate(labele):
            row, row_pred = potr[i], pred[i]
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

    Koristi config VT_OD/VT_DO (zimski sati; ljeti +1h) i važeći cjenik.
    """
    try:
        vt_od = int(get_config('TARIFA_VT_OD', '7'))
        vt_do = int(get_config('TARIFA_VT_DO', '21'))
    except (TypeError, ValueError):
        vt_od, vt_do = 7, 21
    je_vt = vt_sql('ts', vt_od, vt_do)

    conn = get_db()
    try:
        rows = conn.execute(f'''
            SELECT substr(ts,1,7) as mj,
                   ROUND(SUM(CASE WHEN {je_vt} THEN kwh_plus ELSE 0 END), 2) as vt_kwh,
                   ROUND(SUM(CASE WHEN {je_vt} THEN 0 ELSE kwh_plus END), 2) as nt_kwh,
                   ROUND(SUM(kwh_minus), 2) as predaja_kwh
            FROM ocitanja_satna
            WHERE ts >= {sql_now_local('-12 months')}
            GROUP BY substr(ts,1,7)
            ORDER BY mj DESC
        ''').fetchall()

        t = get_tarifa()
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
    VT_OD/VT_DO su zimski (CET) sati; za ljetnog računanja VT je sat kasnije
    po lokalnom vremenu (08-22) — vidi timeutil.vt_sql.

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

    je_vt = vt_sql('ts', vt_od, vt_do)

    conn = get_db()
    try:
        if mjesec:
            try:
                where = "WHERE ts >= ? AND ts < ?"
                params = raspon_mjeseca(mjesec)
            except ValueError:
                where = "WHERE substr(ts,1,7) = ?"
                params = (mjesec,)
            group  = ""
            limit  = ""
        else:
            where = f"WHERE ts >= {sql_now_local('-12 months')}"
            params = ()
            group  = "GROUP BY substr(ts,1,7)"
            limit  = "ORDER BY mj DESC LIMIT 12"

        sql = f'''
            SELECT substr(ts,1,7) as mj,
                   ROUND(SUM(CASE WHEN {je_vt} THEN kwh_plus ELSE 0 END), 2) as vt_simple,
                   ROUND(SUM(CASE WHEN CAST(strftime('%w', ts) AS INT) BETWEEN 1 AND 5
                                   AND {je_vt}
                              THEN kwh_plus ELSE 0 END), 2) as vt_workdays,
                   ROUND(SUM(kwh_plus), 2) as ukupno
            FROM ocitanja_satna
            {where}
            {group}
            {limit}
        '''
        rows = conn.execute(sql, params).fetchall()

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
                'simple':   f'VT = svi dani u satima {vt_od}-{vt_do}h (ljeti {vt_od + 1}-{vt_do + 1}h)',
                'workdays': f'VT = pon–pet u satima {vt_od}-{vt_do}h (ljeti {vt_od + 1}-{vt_do + 1}h), vikend cijeli NT',
            },
        })
    finally:
        conn.close()
