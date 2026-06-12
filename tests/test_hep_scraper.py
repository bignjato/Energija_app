"""Testovi HEP scraper parsiranja i pohrane (bez mreze)."""

import sqlite3

import hep_scraper


def _conn():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    hep_scraper.init_db(conn)
    return conn


def test_parse_vrijednost():
    assert hep_scraper.parse_vrijednost('1,64800000') == 1.648
    assert hep_scraper.parse_vrijednost('1648') == 1648.0
    assert hep_scraper.parse_vrijednost(None) == 0.0
    assert hep_scraper.parse_vrijednost('n/a') == 0.0


def test_get_mjeseci_format():
    mjeseci = hep_scraper.get_mjeseci_za_dohvat(40)
    assert len(mjeseci) >= 2
    for m in mjeseci:
        mm, yyyy = m.split('.')
        assert 1 <= int(mm) <= 12
        assert len(yyyy) == 4
    assert len(mjeseci) == len(set(mjeseci))


def test_spremi_krivulje_dijeli_s_4_i_preskace_buducnost():
    conn = _conn()
    podaci = [
        {'Datum': '2026-01-15T10:00:00', 'Value': '4,0'},
        {'Datum': '2099-01-01T00:00:00', 'Value': '8,0'},  # buducnost — preskoci
    ]
    n = hep_scraper.spremi_krivulje(conn, podaci, 'OMM1', je_minus=False)
    assert n == 1
    row = conn.execute('SELECT kwh_plus FROM ocitanja_15min').fetchone()
    assert row['kwh_plus'] == 1.0  # 4,0 / 4


def test_spremi_krivulje_minus_ne_gazi_plus():
    conn = _conn()
    ts = '2026-01-15T10:00:00'
    hep_scraper.spremi_krivulje(conn, [{'Datum': ts, 'Value': '4,0'}], 'OMM1', je_minus=False)
    hep_scraper.spremi_krivulje(conn, [{'Datum': ts, 'Value': '8,0'}], 'OMM1', je_minus=True)
    row = conn.execute('SELECT kwh_plus, kwh_minus FROM ocitanja_15min').fetchone()
    assert row['kwh_plus'] == 1.0
    assert row['kwh_minus'] == 2.0


def test_agregacija_brise_trailing_nule():
    conn = _conn()
    hep_scraper.spremi_krivulje(conn, [
        {'Datum': '2026-01-15T10:00:00', 'Value': '4,0'},
        {'Datum': '2026-01-16T10:00:00', 'Value': '0'},
        {'Datum': '2026-01-17T10:00:00', 'Value': '0'},
    ], 'OMM1', je_minus=False)
    hep_scraper.agregacija(conn, 'OMM1')
    dani = [r['datum'] for r in conn.execute(
        'SELECT datum FROM ocitanja_dnevna ORDER BY datum')]
    assert dani == ['2026-01-15']
