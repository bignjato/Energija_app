"""Testovi izracuna racuna (standardni + HEPI bijeli net-metering)."""

import sqlite3

import pytest

from hepapp.tariff import (
    HEP_TARIFA,
    get_vt_udio_registri,
    izracunaj_racun,
    izracunaj_racun_bijeli,
    otkup_viskova_bijeli,
)


def test_izracunaj_racun_standardni():
    # 1000 kWh, bez predaje, 30 dana, VT udio 45% — rucno izracunata vrijednost
    assert izracunaj_racun(1000, 0, 30, vt_udio=0.45) == pytest.approx(180.78, abs=0.01)


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
