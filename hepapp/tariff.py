"""HEP tarifa konstante + procjena računa."""

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
    'vt_udio':     0.45,
    'otkup':       0.064379,
}


def izracunaj_racun(kwh_plus, kwh_minus, n_dana=30):
    t = HEP_TARIFA
    vt = kwh_plus * t['vt_udio']
    nt = kwh_plus * (1 - t['vt_udio'])
    n_mj = n_dana / 30.0
    opskrba = (vt * t['vt_opskrba'] + nt * t['nt_opskrba'] +
               kwh_plus * (t['solidarna'] + t['oie']) +
               t['opskrbna_mj'] * n_mj -
               kwh_minus * t['otkup'])
    mreza = (vt * (t['vt_distrib'] + t['vt_prijenos']) +
             nt * (t['nt_distrib'] + t['nt_prijenos']) +
             t['mjerna_mj'] * n_mj)
    return round((opskrba + mreza) * (1 + t['pdv']), 2)
