"""Testovi multi-OMM filtra i /api/omm endpointa."""

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def client(prijavljeni_klijent):
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
        INSERT INTO config (key, value) VALUES ('_setup_complete', '1');
        CREATE TABLE mjerna_mjesta (id TEXT PRIMARY KEY, naziv TEXT, adresa TEXT, tip TEXT);
        CREATE TABLE ocitanja_satna (mjerno_mjesto TEXT, ts TEXT, kwh_plus REAL, kwh_minus REAL,
                                     UNIQUE(mjerno_mjesto, ts));
        CREATE TABLE ocitanja_dnevna (mjerno_mjesto TEXT, datum TEXT, kwh_plus REAL, kwh_minus REAL,
                                      UNIQUE(mjerno_mjesto, datum));
        INSERT INTO mjerna_mjesta VALUES ('OMM1','Kuća','Adresa 1','Potrosac');
        INSERT INTO mjerna_mjesta VALUES ('OMM2','Vikendica','Adresa 2','Potrosac');
        -- OMM2 ima vise dana => mora biti prvi (zadano) u listi
        INSERT INTO ocitanja_dnevna VALUES ('OMM1','2026-06-10', 10, 2);
        INSERT INTO ocitanja_dnevna VALUES ('OMM2','2026-06-10', 100, 20);
        INSERT INTO ocitanja_dnevna VALUES ('OMM2','2026-06-09', 90, 15);
    ''')
    conn.commit(); conn.close()

    # get_db()/get_config() citaju hepapp.db.DB_PATH globalno na svaki poziv
    yield prijavljeni_klijent(path)
    os.unlink(path)


def test_omm_list_sortiran_po_kolicini(client):
    d = client.get('/api/omm').get_json()
    assert [o['id'] for o in d['omm']] == ['OMM2', 'OMM1']  # OMM2 ima vise dana
    assert d['zadano'] == 'OMM2'
    assert d['omm'][0]['naziv'] == 'Vikendica'


def test_data_bez_omm_agregira_sve(client):
    d = client.get('/api/data').get_json()
    dan = next(r for r in d['dnevna'] if r['datum'] == '2026-06-10')
    assert dan['kwh_plus'] == 110   # 10 + 100


def test_data_s_omm_filtrira(client):
    d = client.get('/api/data?omm=OMM2').get_json()
    dan = next(r for r in d['dnevna'] if r['datum'] == '2026-06-10')
    assert dan['kwh_plus'] == 100


def test_povijest_s_omm(client):
    d = client.get('/api/povijest?od=2026-06-01&do=2026-06-30&res=day&omm=OMM1').get_json()
    dan = next(r for r in d['hep'] if r['datum'] == '2026-06-10')
    assert dan['kwh_plus'] == 10


def test_omm_nepoznat_vraca_prazno(client):
    d = client.get('/api/data?omm=NEPOSTOJI').get_json()
    assert not any(r['datum'] == '2026-06-10' for r in d['dnevna'])


# ===== lokalno "sada", sati samo s predajom, SMA UTC -> lokalno =====

from datetime import datetime, timedelta, timezone

from hepapp.timeutil import TZ, now_local

_F = '%Y-%m-%dT%H:%M:%S'


def _sql(stmt, rows=()):
    import hepapp.db as db
    conn = sqlite3.connect(db.DB_PATH)
    if stmt.lstrip().upper().startswith('CREATE'):
        conn.executescript(stmt)
    else:
        conn.executemany(stmt, rows)
    conn.commit(); conn.close()


def _lokalni_sat(delta_h):
    """Lokalni naivni ts na puni sat, pomaknut za delta_h sati od sada."""
    t = now_local().replace(tzinfo=None, minute=0, second=0, microsecond=0)
    return (t + timedelta(hours=delta_h)).strftime(_F)


def test_data_satna_ukljucuje_danasnje_redove(client):
    """Regresija: 'T' ts današnjeg dana leksički je > datetime('now'), pa je
    cijeli tekući dan nestajao iz satne serije. Budući redovi i dalje ne ulaze."""
    prosli, buduci = _lokalni_sat(0), _lokalni_sat(2)
    _sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, ?, ?)", [
        (prosli, 1.5, 0.0),
        (buduci, 9.0, 0.0),
    ])
    ts = [r['ts'] for r in client.get('/api/data').get_json()['satna']]
    assert prosli in ts
    assert buduci not in ts


def test_data_satna_ukljucuje_sat_samo_s_predajom(client):
    """kwh_plus = 0 i kwh_minus > 0 (sunčan sat) je red s podacima; 0/0 nije."""
    _sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, ?, ?)", [
        ('2026-06-10T11:00:00', 0.8, 0.0),
        ('2026-06-10T12:00:00', 0.0, 2.4),     # samo predaja
        ('2026-06-10T13:00:00', 0.0, 0.0),     # prazno
        ('2026-06-10T14:00:00', None, None),     # bez vrijednosti
    ])
    satna = {r['ts']: r for r in client.get('/api/data').get_json()['satna']}
    assert set(satna) == {'2026-06-10T11:00:00', '2026-06-10T12:00:00'}
    assert satna['2026-06-10T12:00:00']['kwh_minus'] == 2.4
    assert satna['2026-06-10T12:00:00']['kwh_plus'] == 0


def test_data_satna_omm_filter_i_dalje_radi(client):
    _sql("INSERT INTO ocitanja_satna VALUES (?, ?, ?, ?)", [
        ('OMM1', '2026-06-10T12:00:00', 0.0, 1.0),
        ('OMM2', '2026-06-10T12:00:00', 3.0, 0.0),
    ])
    sve = client.get('/api/data').get_json()['satna']
    assert sve == [{'ts': '2026-06-10T12:00:00', 'kwh_plus': 3.0, 'kwh_minus': 1.0}]
    samo1 = client.get('/api/data?omm=OMM1').get_json()['satna']
    assert samo1 == [{'ts': '2026-06-10T12:00:00', 'kwh_plus': 0.0, 'kwh_minus': 1.0}]


def test_data_dnevna_ukljucuje_lokalni_danas(client):
    danas = now_local().strftime('%Y-%m-%d')
    sutra = (now_local() + timedelta(days=1)).strftime('%Y-%m-%d')
    _sql("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, ?, ?)", [(danas, 5, 1), (sutra, 7, 1)])
    for url in ('/api/data', '/api/data/sve'):
        datumi = [r['datum'] for r in client.get(url).get_json()['dnevna']]
        assert danas in datumi and sutra not in datumi, url


def test_data_ts_je_lokalni_naivni(client):
    ts = client.get('/api/data').get_json()['ts']
    t = datetime.fromisoformat(ts)
    assert t.tzinfo is None
    assert abs(t - now_local().replace(tzinfo=None)) < timedelta(seconds=10)


_SMA_LIVE = '''
    CREATE TABLE sma_live (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT,
        pv_generation_w REAL, feed_in_w REAL, external_consumption_w REAL,
        total_consumption_w REAL, direct_consumption_w REAL,
        autarky_rate REAL, self_consumption_rate REAL);
    CREATE TABLE sma_dnevna (datum TEXT PRIMARY KEY, pv_generation_kwh REAL, feed_in_kwh REAL,
        grid_consumption_kwh REAL, total_consumption_kwh REAL, self_consumption_kwh REAL,
        autarky_rate REAL, pv_kwh_inv1 REAL, pv_kwh_inv2 REAL);
'''


def _utc_live(lokalno):
    """Lokalni aware datetime -> sma_live format (UTC, '+00:00')."""
    return lokalno.astimezone(timezone.utc).strftime(_F) + '+00:00'


def test_data_sma_today_po_lokalnom_danu(client):
    """sma_live.ts je UTC: lokalnih 00:10-00:40 danas (UTC datum još jučer) ulazi
    u "danas", a jučerašnjih 23:50 lokalno ne."""
    _sql(_SMA_LIVE)
    ponoc = now_local().replace(hour=0, minute=0, second=0, microsecond=0)
    _sql("INSERT INTO sma_live (ts, pv_generation_w, feed_in_w, external_consumption_w, "
         "total_consumption_w, autarky_rate) VALUES (?, ?, ?, ?, ?, ?)", [
        (_utc_live(ponoc - timedelta(minutes=10)), 99999, 99999, 99999, 99999, 0.0),
        (_utc_live(ponoc + timedelta(minutes=10)), 1000, 0, 500, 2000, 0.5),
        (_utc_live(ponoc + timedelta(minutes=40)), 3000, 0, 500, 2000, 0.5),
    ])
    d = client.get('/api/data').get_json()
    st = d['sma_today']
    assert st['n_samples'] == 2
    assert st['proteklo_h'] == 0.5
    assert st['pv_generation_kwh'] == 1.0      # prosjek 2000 W × 0.5 h
    assert st['autarky_rate'] == 0.5


def test_data_sma_satna_zadnjih_7_dana(client):
    """Granice prema UTC 'T' formatu: svježi red ulazi, stariji od 7 dana ne."""
    _sql(_SMA_LIVE)
    sada = now_local()
    _sql("INSERT INTO sma_live (ts, pv_generation_w) VALUES (?, ?)", [
        (_utc_live(sada - timedelta(minutes=2)), 1500),
        (_utc_live(sada - timedelta(days=8)), 7777),
    ])
    sma_satna = client.get('/api/data').get_json()['sma_satna']
    assert len(sma_satna) == 1
    assert sma_satna[0]['pv_w'] == 1500
    # oznaka sata je lokalna
    assert sma_satna[0]['sat'] == (sada - timedelta(minutes=2)).strftime('%Y-%m-%dT%H:00:00')


def test_povijest_hour_granice_i_buduci_redovi(client):
    _sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, ?, ?)", [
        ('2026-06-09T23:00:00', 9.0, 0.0),     # dan prije
        ('2026-06-10T00:00:00', 1.0, 0.0),
        ('2026-06-10T12:00:00', 0.0, 2.0),     # samo predaja
        ('2026-06-11T23:00:00', 3.0, 0.0),
        ('2026-06-12T00:00:00', 9.0, 0.0),     # dan poslije
    ])
    d = client.get('/api/povijest?od=2026-06-10&do=2026-06-11&res=hour').get_json()
    assert [r['ts'] for r in d['hep']] == [
        '2026-06-10T00:00:00', '2026-06-10T12:00:00', '2026-06-11T23:00:00']

    # današnji protekli sat ulazi, budući ne
    prosli, buduci = _lokalni_sat(0), _lokalni_sat(30)
    _sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, ?, ?)", [(prosli, 1.0, 0.0), (buduci, 1.0, 0.0)])
    d = client.get(f'/api/povijest?od={prosli[:10]}&do={buduci[:10]}&res=hour').get_json()
    ts = [r['ts'] for r in d['hep']]
    assert prosli in ts and buduci not in ts


def test_povijest_hour_neispravan_datum_ne_puca(client):
    r = client.get('/api/povijest?od=2026-06&do=xx&res=hour')
    assert r.status_code == 200 and r.get_json()['hep'] == []


def test_povijest_hour_sma_lokalni_sat(client):
    """sma_15min.ts je UTC ('Z'): filtrira se po lokalnom danu, a `sat` je lokalni
    da se u UI-u poklopi s HEP ts ključem."""
    _sql('''CREATE TABLE sma_15min (ts TEXT PRIMARY KEY, pv_w_inv1 REAL, pv_w_inv2 REAL,
                                    pv_w_total REAL);''')
    _sql("INSERT INTO sma_15min VALUES (?, ?, ?, ?)", [
        ('2026-07-14T21:45:00Z', 0, 0, 9999),     # 23:45 lokalno 14.7. — ne ulazi
        ('2026-07-14T22:30:00Z', 0, 0, 4000),     # 00:30 lokalno 15.7.
        ('2026-07-15T10:00:00Z', 1000, 1000, 2000),
        ('2026-07-15T10:15:00Z', 1000, 1000, 2000),
        ('2026-07-15T22:15:00Z', 0, 0, 9999),     # 00:15 lokalno 16.7. — ne ulazi
    ])
    d = client.get('/api/povijest?od=2026-07-15&do=2026-07-15&res=hour').get_json()
    assert [(r['sat'], r['pv_kwh']) for r in d['sma']] == [
        ('2026-07-15T00:00:00', 1.0),
        ('2026-07-15T12:00:00', 1.0),
    ]


def test_povijest_hour_rubni_datumi_ne_pucaju(client):
    """Datumi na rubu raspona datetime-a (ispravnog oblika) ne smiju dati 500:
    SMA upit pada na rezervni predikat po lokalnom danu."""
    _sql('''CREATE TABLE sma_15min (ts TEXT PRIMARY KEY, pv_w_inv1 REAL, pv_w_inv2 REAL,
                                    pv_w_total REAL);''')
    _sql("INSERT INTO sma_15min VALUES (?, ?, ?, ?)", [
        ('2026-07-15T10:00:00Z', 1000, 1000, 2000),
        ('2026-07-15T10:15:00Z', 1000, 1000, 2000),
    ])
    for upit in ('od=0001-01-01&do=2026-11-16', 'od=2026-01-01&do=9999-12-31'):
        r = client.get(f'/api/povijest?{upit}&res=hour')
        assert r.status_code == 200, upit
        assert [(x['sat'], x['pv_kwh']) for x in r.get_json()['sma']] == [
            ('2026-07-15T12:00:00', 1.0)], upit


def test_povijest_default_raspon_je_lokalni(client):
    d = client.get('/api/povijest').get_json()
    assert d['do'] == now_local().strftime('%Y-%m-%d')
    assert d['od'] == (now_local() - timedelta(days=30)).strftime('%Y-%m-%d')
    assert now_local().tzinfo is TZ


def test_efektivne_cijene_iz_vazeceg_cjenika(client):
    from hepapp.tariff import get_tarifa
    ec = client.get('/api/data').get_json()['efektivne_cijene']
    assert ec['kupnja_avg'] > 0
    assert ec['prodaja'] == round(get_tarifa()['otkup'], 6)
