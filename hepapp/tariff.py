"""HEP tarifa konstante + procjena računa.

Podržava dva modela:
  - 'standardni' (default): klasično dvotarifno; potrošnja × cijena − predaja × otkup
  - 'bijeli' (HEPI bijeli / E-K-N-BIJ1): net-metering, saldiranje po tarifi mjesečno;
    samo pozitivni saldo se plaća, viškovi otkupljuju zasebnim transferom.

VT_UDIO_PERC se može override-ati preko config tablice (key: VT_UDIO_PERC, 0–100).
Tipično: ljeto ~35, prosjek ~45, zima ~55.
"""

from datetime import date, datetime

from .db import get_config

# Stavke koje se nisu mijenjale između cjenika (mreža, naknade, PDV).
_ZAJEDNICKO = {
    'vt_distrib':  0.044446,
    'nt_distrib':  0.020514,
    'vt_prijenos': 0.021256,
    'nt_prijenos': 0.008175,
    'solidarna':   0.003982,   # obračunava se pa u cijelosti vraća ("popust na solidarnu naknadu")
    'oie':         0.013239,
    'opskrbna_mj': 0.982,
    'mjerna_mj':   1.983,
    'pdv':         0.13,
    'vt_udio':     0.45,  # default za standardni model (može se promijeniti u Postavkama)
}

# Cjenici po datumu važenja — NAJNOVIJI PRVI. Kad HEP promijeni cijene, dodaj
# novi red na vrh (ne mijenjaj stare: povijesni mjeseci se računaju po cjeniku
# koji je tada vrijedio). Svaki cjenik je provjeren protiv stvarnog računa —
# vidi tests/test_tariff.py (test_stvarni_racun_*).
TARIFE = [
    # Od 1.4.2026. — potvrđeno računima 04–10/2026 (npr. 10/2026: 63,39 €).
    ('2026-04-01', {
        **_ZAJEDNICKO,
        'vt_opskrba': 0.077751,
        'nt_opskrba': 0.047688,
        # Otkup VT viška = puna VT cijena opskrbe (račun 10/2026: 546 kWh → 42,45 €).
        'vt_otkup':   0.077751,
        'nt_otkup':   0.047688,   # pretpostavka po analogiji — NT viška na računima još nije bilo
        'otkup':      0.077751,   # standardni model: solarni višak nastaje u VT satima
    }),
    # Do 31.3.2026. — potvrđeno računima 01–03/2026 (npr. 03/2026: opskrba 49,18 €).
    ('1900-01-01', {
        **_ZAJEDNICKO,
        'vt_opskrba': 0.131205,
        'nt_opskrba': 0.064379,
        'vt_otkup':   0.104964,
        'nt_otkup':   0.064379,
        'otkup':      0.064379,
    }),
]


def get_tarifa(datum=None) -> dict:
    """Cjenik koji vrijedi na zadani datum.

    datum: None (danas), 'YYYY-MM-DD', 'YYYY-MM' (uzima se 1. u mjesecu),
           ili date/datetime objekt.
    """
    if datum is None:
        d = date.today().isoformat()
    elif isinstance(datum, (date, datetime)):
        d = datum.strftime('%Y-%m-%d')
    else:
        d = str(datum)[:10]
        if len(d) == 7:
            d += '-01'
    for vrijedi_od, cjenik in TARIFE:
        if d >= vrijedi_od:
            return cjenik
    return TARIFE[-1][1]


# Trenutno važeći cjenik (kompatibilnost sa starim pozivateljima).
HEP_TARIFA = get_tarifa()


def get_vt_udio() -> float:
    """Dohvati trenutni VT udio (0–1). Default 0.45."""
    try:
        v = float(get_config('VT_UDIO_PERC', '45'))
        if 0 <= v <= 100:
            return v / 100.0
    except Exception:
        pass
    return 0.45


def get_vt_udio_registri(conn):
    """Sluzbeni VT udjeli iz HEP registara (delta mjesecnih stanja brojila).

    Vraca (po_mjesecu, prosjek):
      po_mjesecu: {'YYYY-MM': vt_udio 0-1} za svaki mjesec s oba stanja
      prosjek:    potrosnjom vagani prosjek zadnja 3 mjeseca (ili None)
    """
    try:
        rows = conn.execute(
            "SELECT datum, obis, value FROM hep_registri "
            "WHERE obis IN ('A+_T1','A+_T2') ORDER BY datum"
        ).fetchall()
    except Exception:
        return {}, None
    stanja = {}
    for r in rows:
        stanja.setdefault(r['datum'], {})[r['obis']] = r['value']
    datumi = sorted(d for d, v in stanja.items()
                    if v.get('A+_T1') is not None and v.get('A+_T2') is not None)
    po_mjesecu = {}
    delte = []
    for prev, cur in zip(datumi, datumi[1:]):
        dvt = stanja[cur]['A+_T1'] - stanja[prev]['A+_T1']
        dnt = stanja[cur]['A+_T2'] - stanja[prev]['A+_T2']
        uk = dvt + dnt
        if dvt < 0 or dnt < 0 or uk <= 0:
            continue  # zamjena brojila i sl.
        po_mjesecu[cur[:7]] = dvt / uk
        delte.append((dvt, uk))
    prosjek = None
    zadnje = delte[-3:]
    if zadnje:
        sum_vt = sum(d[0] for d in zadnje)
        sum_uk = sum(d[1] for d in zadnje)
        if sum_uk > 0:
            prosjek = sum_vt / sum_uk
    return po_mjesecu, prosjek


def get_tariff_model() -> str:
    """Vrati 'bijeli' ili 'standardni' iz config-a."""
    return (get_config('TARIFA_MODEL', 'standardni') or 'standardni').lower()


def izracunaj_racun(kwh_plus, kwh_minus, n_dana=30, vt_udio=None, datum=None):
    """Standardni dvotarifni model — približna formula s VT_UDIO slider-om.

    datum: po kojem cjeniku računati (None = trenutni); vidi `get_tarifa`.
    """
    t = get_tarifa(datum)
    if vt_udio is None:
        vt_udio = get_vt_udio()
    vt = kwh_plus * vt_udio
    nt = kwh_plus * (1 - vt_udio)
    n_mj = n_dana / 30.0
    opskrba = (vt * t['vt_opskrba'] + nt * t['nt_opskrba'] +
               kwh_plus * (t['solidarna'] + t['oie']) +
               t['opskrbna_mj'] * n_mj -
               kwh_minus * t['otkup'])
    # Popust na solidarnu naknadu — na računu se obračuna pa u cijelosti vrati.
    opskrba -= kwh_plus * t['solidarna']
    mreza = (vt * (t['vt_distrib'] + t['vt_prijenos']) +
             nt * (t['nt_distrib'] + t['nt_prijenos']) +
             t['mjerna_mj'] * n_mj)
    return round((opskrba + mreza) * (1 + t['pdv']), 2)


def izracunaj_racun_bijeli(vt_pot, nt_pot, vt_pred, nt_pred, n_dana=30, datum=None):
    """HEPI bijeli (net-metering) formula.

    datum: po kojem cjeniku računati (None = trenutni); vidi `get_tarifa`.

    Mjesečno saldiranje po tarifi:
        saldo_vt = vt_pot - vt_pred   (pozitivno = neto kupili VT)
        saldo_nt = nt_pot - nt_pred   (pozitivno = neto kupili NT)

    Plaća se SAMO pozitivni saldo. Negativni saldo se otkupljuje
    posebnim transferom (ne ulazi u račun); vidi `otkup_viskova_bijeli`.

    Vraća dict:
        iznos       — ukupno za platiti (osnovica + PDV, bez kamata)
        osnovica    — opskrba + mreža
        opskrba, mreza, pdv  — komponente
        saldo_vt, saldo_nt
    """
    t = get_tarifa(datum)
    saldo_vt = (vt_pot or 0) - (vt_pred or 0)
    saldo_nt = (nt_pot or 0) - (nt_pred or 0)
    pos_vt = max(0, saldo_vt)
    pos_nt = max(0, saldo_nt)
    n_mj = n_dana / 30.0

    opskrba = (
        pos_vt * t['vt_opskrba'] +
        pos_nt * t['nt_opskrba'] +
        (pos_vt + pos_nt) * (t['solidarna'] + t['oie']) +
        t['opskrbna_mj'] * n_mj
    )
    # Popust na solidarnu (vidi PDF: "popust na solidarnu naknadu") — simetrično je s naknadom
    popust_solidarna = -(pos_vt + pos_nt) * t['solidarna']
    opskrba += popust_solidarna

    mreza = (
        pos_vt * (t['vt_distrib'] + t['vt_prijenos']) +
        pos_nt * (t['nt_distrib'] + t['nt_prijenos']) +
        t['mjerna_mj'] * n_mj
    )

    osnovica = opskrba + mreza
    pdv = osnovica * t['pdv']
    iznos = round(osnovica + pdv, 2)

    return {
        'iznos':    iznos,
        'osnovica': round(osnovica, 2),
        'opskrba':  round(opskrba, 2),
        'mreza':    round(mreza, 2),
        'pdv':      round(pdv, 2),
        'saldo_vt': round(saldo_vt, 2),
        'saldo_nt': round(saldo_nt, 2),
    }


def otkup_viskova_bijeli(vt_pot, nt_pot, vt_pred, nt_pred, datum=None):
    """Otkup za HEPI bijeli — viškovi (negativni saldo) × otkupna cijena.

    datum: po kojem cjeniku računati (None = trenutni); vidi `get_tarifa`.
    Vraća dict: vt_kwh, nt_kwh, vt_eur, nt_eur, ukupno_eur.
    """
    t = get_tarifa(datum)
    vt_visak = max(0, (vt_pred or 0) - (vt_pot or 0))
    nt_visak = max(0, (nt_pred or 0) - (nt_pot or 0))
    vt_eur = round(vt_visak * t['vt_otkup'], 2)
    nt_eur = round(nt_visak * t['nt_otkup'], 2)
    return {
        'vt_kwh':     vt_visak,
        'nt_kwh':     nt_visak,
        'vt_eur':     vt_eur,
        'nt_eur':     nt_eur,
        'ukupno_eur': round(vt_eur + nt_eur, 2),
    }
