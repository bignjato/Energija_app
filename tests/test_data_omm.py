"""Testovi multi-OMM filtra i /api/omm endpointa."""

import os
import sqlite3
import tempfile

import pytest


@pytest.fixture
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    monkeypatch.setenv('SECRET_KEY', '0123456789abcdef0123456789abcdef')

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
    import hepapp.db as db
    monkeypatch.setattr(db, 'DB_PATH', path)
    from app import app
    app.config.update(TESTING=True)

    c = app.test_client()
    with c.session_transaction() as s:
        s['logged_in'] = True
        s['uloga'] = 'admin'
    yield c
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
