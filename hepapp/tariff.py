"""HEP tarifa konstante + procjena računa.

Podržava dva modela:
  - 'standardni' (default): klasično dvotarifno; potrošnja × cijena − predaja × otkup
  - 'bijeli' (HEPI bijeli / E-K-N-BIJ1): net-metering, saldiranje po tarifi mjesečno;
    samo pozitivni saldo se plaća, viškovi otkupljuju zasebnim transferom.

VT_UDIO_PERC se može override-ati preko config tablice (key: VT_UDIO_PERC, 0–100).
Tipično: ljeto ~35, prosjek ~45, zima ~55.
"""

from .db import get_config

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
    'vt_udio':     0.45,  # default za standardni model (može se promijeniti u Postavkama)
    'otkup':       0.064379,    # default otkup (standardni model)
    'vt_otkup':    0.104964,    # HEPI bijeli: otkup VT viškova
    'nt_otkup':    0.064379,    # HEPI bijeli: otkup NT viškova
}


def get_vt_udio() -> float:
    """Dohvati trenutni VT udio (0–1). Default 0.45."""
    try:
        v = float(get_config('VT_UDIO_PERC', '45'))
        if 0 <= v <= 100:
            return v / 100.0
    except Exception:
        pass
    return 0.45


def get_tariff_model() -> str:
    """Vrati 'bijeli' ili 'standardni' iz config-a."""
    return (get_config('TARIFA_MODEL', 'standardni') or 'standardni').lower()


def izracunaj_racun(kwh_plus, kwh_minus, n_dana=30, vt_udio=None):
    """Standardni dvotarifni model — približna formula s VT_UDIO slider-om."""
    t = HEP_TARIFA
    if vt_udio is None:
        vt_udio = get_vt_udio()
    vt = kwh_plus * vt_udio
    nt = kwh_plus * (1 - vt_udio)
    n_mj = n_dana / 30.0
    opskrba = (vt * t['vt_opskrba'] + nt * t['nt_opskrba'] +
               kwh_plus * (t['solidarna'] + t['oie']) +
               t['opskrbna_mj'] * n_mj -
               kwh_minus * t['otkup'])
    mreza = (vt * (t['vt_distrib'] + t['vt_prijenos']) +
             nt * (t['nt_distrib'] + t['nt_prijenos']) +
             t['mjerna_mj'] * n_mj)
    return round((opskrba + mreza) * (1 + t['pdv']), 2)


def izracunaj_racun_bijeli(vt_pot, nt_pot, vt_pred, nt_pred, n_dana=30):
    """HEPI bijeli (net-metering) formula.

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
    t = HEP_TARIFA
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


def otkup_viskova_bijeli(vt_pot, nt_pot, vt_pred, nt_pred):
    """Otkup za HEPI bijeli — viškovi (negativni saldo) × otkupna cijena.

    Vraća dict: vt_kwh, nt_kwh, vt_eur, nt_eur, ukupno_eur.
    """
    t = HEP_TARIFA
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
