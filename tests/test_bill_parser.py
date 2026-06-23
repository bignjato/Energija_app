"""Testovi za HEP PDF parser (parse_hep_bill nad sintetickim tekstom racuna).

Tekst je slozen prema stvarnom HEPI bijeli (E-K-N-BIJ1) layoutu koji
pdfplumber izvuce iz PDF-a — ako HEP promijeni format, ovi testovi pucaju
prvi i pokazu koje polje je prestalo raditi.
"""

import pytest

from hepapp.bill_parser import _to_float, missing_key_fields, parse_hep_bill

SAMPLE = """HEP ELEKTRA d.o.o.
Šifra kupca: 1234567
Broj računa: 1234567-2026-04
Model: HEPI bijeli
tarifni model: E-K-N-BIJ1
Datum računa: 05.05.2026
Razdoblje: 01.04.2026 - 30.04.2026
viša tarifa - potrošnja 549
niža tarifa - potrošnja 622
viša tarifa - proizvodnja 320
niža tarifa - proizvodnja 95
opskrbna naknada po 0,982 EUR/mjesec
naknada za mjernu uslugu (br.mjeseci) po 1,983 EUR
Ukupni iznos računa za 4/2026
Ukupan iznos za opskrbu 49,18
Ukupni iznos za korištenje mreže i usluga 19,80 € 79,10 €
PDV 13% (osnovica: 68,98) 8,97 €
Kamata - PDV nije obračunat 1,15 €
Vaš dug 1.054,72 €
Platite do: 20.05.2026
"""


def test_to_float_hr_formati():
    assert _to_float('1.054,72') == 1054.72
    assert _to_float('8,97') == 8.97
    assert _to_float('549') == 549.0
    assert _to_float('79,10 €') == 79.10
    assert _to_float(None) is None
    assert _to_float('n/a') is None


@pytest.fixture(scope='module')
def parsed():
    return parse_hep_bill(SAMPLE)


def test_period_i_razdoblje(parsed):
    assert parsed['period'] == '04/2026'
    assert parsed['od'] == '2026-04-01'
    assert parsed['do'] == '2026-04-30'


def test_iznos_iz_dvostupcanog_layouta(parsed):
    # dva iznosa na liniji — drugi (desni box) je ukupni iznos racuna
    assert parsed['iznos'] == 79.10


def test_potrosnja_vt_nt(parsed):
    assert parsed['kwh_vt'] == 549
    assert parsed['kwh_nt'] == 622
    assert parsed['kwh_plus'] == 1171


def test_proizvodnja(parsed):
    assert parsed['kwh_minus'] == 415  # 320 VT + 95 NT


def test_komponente(parsed):
    assert parsed['opskrba'] == 49.18
    assert parsed['mreza'] == 19.80
    assert parsed['pdv'] == 8.97
    assert parsed['kamata'] == 1.15


def test_dug_i_naknade(parsed):
    assert parsed['dug'] == 1054.72
    assert parsed['pretplata'] == 0.982
    assert parsed['mjerna_mjernina'] == 1.983


def test_metapodaci(parsed):
    assert parsed['datum_racuna'] == '2026-05-05'
    assert parsed['datum_dospijeca'] == '2026-05-20'
    assert parsed['model_tarife'] == 'HEPI bijeli — E-K-N-BIJ1'
    assert parsed['sifra_kupca'] == '1234567'
    assert parsed['broj_racuna'] == '1234567-2026-04'


def test_prazan_tekst_ne_puca():
    res = parse_hep_bill('')
    assert res['iznos'] is None
    assert res['period'] is None


def test_missing_key_fields_potpun_racun(parsed):
    assert missing_key_fields(parsed) == []


def test_missing_key_fields_prazan():
    res = parse_hep_bill('')
    nedostaje = missing_key_fields(res)
    # prazan tekst => sva ključna polja nedostaju
    assert 'ukupni iznos' in nedostaje
    assert 'ukupna potrošnja' in nedostaje
    assert 'PDV' in nedostaje
    assert len(nedostaje) == 6


def test_missing_key_fields_djelomicno():
    # samo iznos i period prepoznati, mreža/pdv/opskrba/potrošnja fale
    txt = ("Ukupni iznos računa za 4/2026\n"
           "Ukupni iznos za korištenje mreže 19,80 € 79,10 €\n")
    res = parse_hep_bill(txt)
    nedostaje = missing_key_fields(res)
    assert 'ukupni iznos' not in nedostaje      # 79,10 prepoznat
    assert 'razdoblje računa' not in nedostaje   # 4/2026 prepoznat
    assert 'PDV' in nedostaje
    assert 'ukupna potrošnja' in nedostaje
