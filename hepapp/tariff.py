"""HEP tarifa konstante + procjena računa.

VT_UDIO se može override-ati preko config tablice (key: VT_UDIO_PERC, vrijednost 0–100).
Sezonski tipično: ljeto ~35, prosjek ~45, zima ~55.
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
    'vt_udio':     0.45,  # default — može se promijeniti u Postavkama (VT_UDIO_PERC)
    'otkup':       0.064379,
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


def izracunaj_racun(kwh_plus, kwh_minus, n_dana=30, vt_udio=None):
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
