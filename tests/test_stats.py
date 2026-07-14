"""Smoke + logika testovi za /api/stats/* endpointe."""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def client(monkeypatch):
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    monkeypatch.setenv('SECRET_KEY', '0123456789abcdef0123456789abcdef')

    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
        INSERT INTO config (key, value) VALUES ('_setup_complete','1');
        INSERT INTO config (key, value) VALUES ('TARIFA_VT_OD','7');
        INSERT INTO config (key, value) VALUES ('TARIFA_VT_DO','21');
        CREATE TABLE ocitanja_satna (mjerno_mjesto TEXT, ts TEXT, kwh_plus REAL, kwh_minus REAL,
                                     kvarh_plus REAL, kvarh_minus REAL, UNIQUE(mjerno_mjesto, ts));
        CREATE TABLE ocitanja_15min (mjerno_mjesto TEXT, ts TEXT, kwh_plus REAL, kwh_minus REAL,
                                     UNIQUE(mjerno_mjesto, ts));
        CREATE TABLE ocitanja_dnevna (mjerno_mjesto TEXT, datum TEXT, kwh_plus REAL, kwh_minus REAL,
                                      UNIQUE(mjerno_mjesto, datum));
        CREATE TABLE racuni (id INTEGER PRIMARY KEY, period TEXT, iznos REAL, kwh_plus REAL,
                             kwh_minus REAL, dug REAL, datum_racuna TEXT, datum_dospijeca TEXT,
                             model_tarife TEXT, pretplata REAL, mjerna_mjernina REAL);
        CREATE TABLE hep_registri (mjerno_mjesto TEXT, datum TEXT, obis TEXT, value REAL,
                                   UNIQUE(mjerno_mjesto, datum, obis));
    ''')
    # zadnjih ~40 dana satnih + dnevnih ocitanja
    base = datetime.now()
    for d in range(40):
        dan = (base - timedelta(days=d)).strftime('%Y-%m-%d')
        conn.execute("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 12.0, 3.0)", (dan,))
        for h in (8, 14, 20):
            ts = f'{dan}T{h:02d}:00:00'
            conn.execute("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, 1.5, 0.4, 0, 0)", (ts,))
            conn.execute("INSERT INTO ocitanja_15min VALUES ('OMM1', ?, 0.5, 0.1)", (ts,))
    # dva mjesecna registra (delta = sluzbeni VT udio)
    conn.execute("INSERT INTO hep_registri VALUES ('OMM1','2026-04-30','A+_T1',1000)")
    conn.execute("INSERT INTO hep_registri VALUES ('OMM1','2026-04-30','A+_T2',1000)")
    conn.execute("INSERT INTO hep_registri VALUES ('OMM1','2026-05-31','A+_T1',1450)")
    conn.execute("INSERT INTO hep_registri VALUES ('OMM1','2026-05-31','A+_T2',1550)")
    conn.commit(); conn.close()

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


def test_mjesecni(client):
    d = client.get('/api/stats/mjesecni').get_json()
    assert 'mjeseci' in d and 'tarifa' in d
    assert any(m['hep_potrosnja'] for m in d['mjeseci'])
    # procjena racuna mora biti izracunata
    assert all('procj_racun' in m for m in d['mjeseci'])


def test_peak_snaga(client):
    d = client.get('/api/stats/peak-snaga').get_json()
    assert 'tjedan' in d and 'mjesec' in d
    # kW = kwh_plus(15min) * 4 = 0.5*4 = 2.0
    assert d['mjesec']['peak_potrosnja_kw'] == 2.0


def test_vt_nt_trosak(client):
    d = client.get('/api/stats/vt-nt-trosak').get_json()
    assert d['vt_od'] == 7 and d['vt_do'] == 21
    assert isinstance(d['mjeseci'], list)


def test_vt_kalibracija(client):
    d = client.get('/api/stats/vt-kalibracija').get_json()
    assert 'mjeseci' in d
    for m in d['mjeseci']:
        assert m['vt_simple_perc'] is None or 0 <= m['vt_simple_perc'] <= 100


def test_registri_delta(client):
    d = client.get('/api/stats/registri').get_json()
    assert d['mjeseci'], 'ocekujem bar jednu mjesecnu deltu'
    svibanj = next(m for m in d['mjeseci'] if m['mjesec'] == '2026-05')
    assert svibanj['vt_kwh'] == 450    # 1450-1000
    assert svibanj['nt_kwh'] == 550    # 1550-1000
    assert svibanj['vt_perc'] == 45.0  # 450/1000


def test_optimalno(client):
    d = client.get('/api/stats/optimalno').get_json()
    assert 'hep_satno' in d
    assert isinstance(d['hep_satno'], list)


def test_dug_prazan(client):
    """Bez računa: trenutni/vrh/pace su None, ali odgovor je valjan (+ cijene)."""
    d = client.get('/api/stats/dug').get_json()
    assert d['racuni'] == []
    assert d['trenutni'] is None
    assert d['vrh'] is None
    assert d['pace'] is None
    assert 'cijene' in d and d['cijene']['vt_kupnja'] > 0


@pytest.fixture
def client_dug(monkeypatch):
    """Kao `client`, ali s 5 računa u silaznom trendu duga (−100 €/mj)."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    monkeypatch.setenv('SECRET_KEY', '0123456789abcdef0123456789abcdef')

    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
        INSERT INTO config (key, value) VALUES ('_setup_complete','1');
        CREATE TABLE ocitanja_dnevna (mjerno_mjesto TEXT, datum TEXT, kwh_plus REAL, kwh_minus REAL,
                                      UNIQUE(mjerno_mjesto, datum));
        CREATE TABLE racuni (id INTEGER PRIMARY KEY, period TEXT, iznos REAL, kwh_plus REAL,
                             kwh_minus REAL, dug REAL, datum_racuna TEXT, datum_dospijeca TEXT,
                             model_tarife TEXT, pretplata REAL, mjerna_mjernina REAL);
        CREATE TABLE hep_registri (mjerno_mjesto TEXT, datum TEXT, obis TEXT, value REAL,
                                   UNIQUE(mjerno_mjesto, datum, obis));
    ''')
    # dug pada 1000 → 600 (Δ −100/mj); standardni model → izbjegava bijeli registri put
    for i, (period, dug) in enumerate([
        ('2026-01', 1000.0), ('2026-02', 900.0), ('2026-03', 800.0),
        ('2026-04', 700.0), ('2026-05', 600.0),
    ]):
        conn.execute(
            "INSERT INTO racuni (period, iznos, dug, datum_racuna, model_tarife) "
            "VALUES (?, ?, ?, ?, 'standardni')",
            (period, 50.0, dug, f'{period}-28'))
    conn.commit(); conn.close()

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


def test_dug_pace(client_dug):
    """pace = prosjek delta zadnja 4 računa = −100.0."""
    d = client_dug.get('/api/stats/dug').get_json()
    assert d['pace'] == -100.0


def test_dug_trenutni_vrh(client_dug):
    """trenutni = zadnji period; vrh = račun s najvećim dugom."""
    d = client_dug.get('/api/stats/dug').get_json()
    assert len(d['racuni']) == 5
    assert d['trenutni']['period'] == '2026-05'
    assert d['trenutni']['dug'] == 600.0
    assert d['vrh']['period'] == '2026-01'
    assert d['vrh']['dug'] == 1000.0
