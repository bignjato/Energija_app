"""Testovi izracuna racuna (standardni + HEPI bijeli net-metering)."""

import sqlite3

import pytest

from hepapp.tariff import (
    HEP_TARIFA,
    TARIFE,
    get_tarifa,
    get_vt_udio_registri,
    izracunaj_racun,
    izracunaj_racun_bijeli,
    otkup_viskova_bijeli,
)


def test_izracunaj_racun_standardni():
    # 1000 kWh, bez predaje, 30 dana, VT udio 45% — rucno izracunate vrijednosti
    # (solidarna naknada se obracuna pa u cijelosti vrati -> ne ulazi u iznos)
    # stari cjenik (do 31.3.2026.)
    assert izracunaj_racun(1000, 0, 30, vt_udio=0.45, datum='2026-01-15') == pytest.approx(176.28, abs=0.01)
    # cjenik od 1.4.2026.
    assert izracunaj_racun(1000, 0, 30, vt_udio=0.45, datum='2026-10-15') == pytest.approx(138.72, abs=0.01)


def test_standardni_solidarna_se_vraca():
    # iznos ne smije ovisiti o visini solidarne naknade (popust je simetrican)
    t = get_tarifa('2026-10-15')
    bez_naknada = (450 * (t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos']) +
                   550 * (t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos']) +
                   1000 * t['oie'] + t['opskrbna_mj'] + t['mjerna_mj'])
    assert izracunaj_racun(1000, 0, 30, vt_udio=0.45, datum='2026-10-15') == \
        pytest.approx(bez_naknada * (1 + t['pdv']), abs=0.01)


# ---------------------------------------------------------------------------
# Cjenici po datumu
# ---------------------------------------------------------------------------

def test_get_tarifa_po_datumu():
    assert get_tarifa('2026-03-31')['nt_opskrba'] == 0.064379
    assert get_tarifa('2026-04-01')['nt_opskrba'] == 0.047688
    assert get_tarifa('2026-03')['vt_opskrba'] == 0.131205      # 'YYYY-MM'
    assert get_tarifa('2026-04')['vt_opskrba'] == 0.077751
    assert get_tarifa('2019-01-01') is TARIFE[-1][1]            # prije svega -> najstariji
    from datetime import date
    assert get_tarifa(date(2026, 10, 31))['vt_otkup'] == 0.077751


def test_tarife_su_sortirane_i_potpune():
    datumi = [d for d, _ in TARIFE]
    assert datumi == sorted(datumi, reverse=True), 'TARIFE mora biti najnoviji prvi'
    kljucevi = set(TARIFE[0][1])
    for d, cjenik in TARIFE:
        assert set(cjenik) == kljucevi, f'cjenik {d} nema iste kljuceve'
    assert HEP_TARIFA is get_tarifa()


# ---------------------------------------------------------------------------
# Regresija protiv STVARNIH HEP racuna (model HEPI bijeli, OMM 0149216862).
# Ako ovi testovi padnu nakon izmjene cijena/formule — izracun se razisao
# od onoga sto HEP stvarno naplacuje. Tolerancija 5 centi pokriva HEP-ovo
# zaokruzivanje po stavci.
# ---------------------------------------------------------------------------

STVARNI_RACUNI = [
    # (period, vt_pot, nt_pot, vt_pred, nt_pred, opskrba, mreza, iznos)
    # 03/2026 — zadnji mjesec starog cjenika. `iznos` na racunu (79,10) ukljucuje
    # i stavku izvan opskrbe/mreze, pa se ovdje provjeravaju samo komponente.
    ('2026-03', 549, 622, 1507, 1, 49.18, 19.80, None),
    # 06/2026 — novi cjenik
    ('2026-06', 295, 480, 2740, 125, 22.61, 12.16, 39.29),
    # 10/2026 — novi cjenik; racun br. 0010288297-261020-8
    ('2026-10', 533, 595, 1079, 2, 37.11, 18.99, 63.39),
]


@pytest.mark.parametrize('period,vt_pot,nt_pot,vt_pred,nt_pred,opskrba,mreza,iznos', STVARNI_RACUNI)
def test_stvarni_racun_bijeli(period, vt_pot, nt_pot, vt_pred, nt_pred, opskrba, mreza, iznos):
    r = izracunaj_racun_bijeli(vt_pot, nt_pot, vt_pred, nt_pred, 30, datum=period)
    assert r['opskrba'] == pytest.approx(opskrba, abs=0.05)
    assert r['mreza'] == pytest.approx(mreza, abs=0.05)
    if iznos is not None:
        assert r['iznos'] == pytest.approx(iznos, abs=0.05)


def test_stvarni_otkup_listopad_2026():
    # "U listopadu smo od Vas otkupili 546 kWh elektricne energije - za 42,45 eura."
    o = otkup_viskova_bijeli(533, 595, 1079, 2, datum='2026-10')
    assert o['vt_kwh'] == 546
    assert o['nt_kwh'] == 0
    assert o['ukupno_eur'] == pytest.approx(42.45, abs=0.01)


def test_stari_cjenik_bi_precijenio_novi_racun():
    # cuvar: racun 10/2026 po starom cjeniku ispada ~18 % skuplji — upravo
    # greska koju je datumsko verzioniranje cjenika ispravilo
    stari = izracunaj_racun_bijeli(533, 595, 1079, 2, 30, datum='2026-03')['iznos']
    assert stari == pytest.approx(63.39 * 1.18, rel=0.02)


def test_izracunaj_racun_predaja_smanjuje_iznos():
    bez = izracunaj_racun(1000, 0, 30, vt_udio=0.45)
    s_predajom = izracunaj_racun(1000, 500, 30, vt_udio=0.45)
    assert s_predajom == pytest.approx(bez - 500 * HEP_TARIFA['otkup'] * 1.13, abs=0.01)


def test_bijeli_pozitivni_saldo():
    r = izracunaj_racun_bijeli(300, 200, 100, 50, 30)
    assert r['saldo_vt'] == 200
    assert r['saldo_nt'] == 150
    assert r['iznos'] == pytest.approx(round(r['osnovica'] * (1 + HEP_TARIFA['pdv']), 2), abs=0.01)
    assert r['osnovica'] == pytest.approx(r['opskrba'] + r['mreza'], abs=0.01)


def test_bijeli_visak_placa_samo_fiksno():
    # predaja > potrosnja u obje tarife — placaju se samo mjesecne naknade
    r = izracunaj_racun_bijeli(100, 100, 500, 500, 30)
    fiksno = (HEP_TARIFA['opskrbna_mj'] + HEP_TARIFA['mjerna_mj']) * (1 + HEP_TARIFA['pdv'])
    assert r['iznos'] == pytest.approx(fiksno, abs=0.01)
    assert r['saldo_vt'] == -400
    assert r['saldo_nt'] == -400


def test_otkup_viskova():
    o = otkup_viskova_bijeli(100, 100, 500, 500)
    assert o['vt_kwh'] == 400
    assert o['nt_kwh'] == 400
    assert o['vt_eur'] == pytest.approx(400 * HEP_TARIFA['vt_otkup'], abs=0.01)
    assert o['nt_eur'] == pytest.approx(400 * HEP_TARIFA['nt_otkup'], abs=0.01)
    assert o['ukupno_eur'] == pytest.approx(o['vt_eur'] + o['nt_eur'], abs=0.01)


def test_otkup_bez_viska_je_nula():
    o = otkup_viskova_bijeli(500, 500, 100, 100)
    assert o['ukupno_eur'] == 0


def test_get_vt_udio_registri():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE hep_registri (datum TEXT, obis TEXT, value REAL)')
    conn.executemany(
        'INSERT INTO hep_registri (datum, obis, value) VALUES (?,?,?)',
        [
            ('2026-03-31', 'A+_T1', 1000.0), ('2026-03-31', 'A+_T2', 1000.0),
            ('2026-04-30', 'A+_T1', 1450.0), ('2026-04-30', 'A+_T2', 1550.0),
        ],
    )
    po_mjesecu, prosjek = get_vt_udio_registri(conn)
    assert po_mjesecu == {'2026-04': pytest.approx(0.45)}
    assert prosjek == pytest.approx(0.45)


def test_get_vt_udio_registri_preskace_zamjenu_brojila():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE hep_registri (datum TEXT, obis TEXT, value REAL)')
    conn.executemany(
        'INSERT INTO hep_registri (datum, obis, value) VALUES (?,?,?)',
        [
            ('2026-03-31', 'A+_T1', 1000.0), ('2026-03-31', 'A+_T2', 1000.0),
            # novo brojilo — stanje resetirano, delta negativna
            ('2026-04-30', 'A+_T1', 10.0), ('2026-04-30', 'A+_T2', 20.0),
        ],
    )
    po_mjesecu, prosjek = get_vt_udio_registri(conn)
    assert po_mjesecu == {}
    assert prosjek is None
