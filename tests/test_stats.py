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


@pytest.fixture
def client_pv(monkeypatch):
    """Registri s A+/A- za net-metering saldo + ROI, uz investiciju u config."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    monkeypatch.setenv('SECRET_KEY', '0123456789abcdef0123456789abcdef')

    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
        INSERT INTO config (key, value) VALUES ('_setup_complete','1');
        INSERT INTO config (key, value) VALUES ('PV_INVESTMENT_EUR','10000');
        INSERT INTO config (key, value) VALUES ('PV_COMMISSION_DATE','2026-04-15');
        CREATE TABLE hep_registri (mjerno_mjesto TEXT, datum TEXT, obis TEXT, value REAL,
                                   UNIQUE(mjerno_mjesto, datum, obis));
    ''')
    # 3 stanja → 2 mjesečne delte
    #  2026-05: uzeto 300+200=500, predano 300+200=500
    #  2026-06: uzeto 200+150=350, predano 400+250=650
    reg = [
        ('2026-04-30', 1000, 1000, 200, 100),
        ('2026-05-31', 1300, 1200, 500, 300),
        ('2026-06-30', 1500, 1350, 900, 550),
    ]
    for datum, apt1, apt2, amt1, amt2 in reg:
        for obis, val in [('A+_T1', apt1), ('A+_T2', apt2), ('A-_T1', amt1), ('A-_T2', amt2)]:
            conn.execute("INSERT INTO hep_registri VALUES ('OMM1', ?, ?, ?)", (datum, obis, val))
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


def test_godisnji_saldo(client_pv):
    d = client_pv.get('/api/stats/godisnji-saldo').get_json()
    t = d['total']
    assert len(d['mjeseci']) == 2
    assert t['uzeto_kwh'] == 850.0     # 500 + 350
    assert t['pred_kwh'] == 1150.0     # 500 + 650
    assert t['neto_saldo_kwh'] == 300.0
    assert t['placeno_eur'] >= 0 and t['otkup_eur'] > 0
    assert t['neto_trosak_eur'] == round(t['placeno_eur'] - t['otkup_eur'], 2)


def test_godisnji_saldo_bez_predaje(client):
    """Samo A+ registri (nema predaje): pred/otkup/korist su 0, uzeto pozitivno."""
    d = client.get('/api/stats/godisnji-saldo').get_json()
    t = d['total']
    assert t is not None
    assert t['uzeto_kwh'] == 1000.0    # (1450-1000)+(1550-1000)
    assert t['pred_kwh'] == 0.0
    assert t['otkup_eur'] == 0.0
    assert d['mjeseci'][0]['korist_eur'] == 0.0


def test_roi(client_pv):
    d = client_pv.get('/api/stats/roi').get_json()
    assert d['investicija_eur'] == 10000.0
    assert d['commission'] == '2026-04-15'
    assert d['n_mjeseci'] == 2
    assert d['korist_ukupno_eur'] > 0
    assert d['godisnja_korist_eur'] == round(d['korist_ukupno_eur'] / 2 * 12, 2)
    assert d['payback_godina'] and d['payback_godina'] > 0
    assert d['pokriveno_perc'] > 0
    # breakeven = commission (2026-04) + payback*12 mjeseci, format YYYY-MM
    assert len(d['breakeven_mjesec']) == 7 and d['breakeven_mjesec'][:4].isdigit()


def test_roi_bez_investicije(client):
    """Bez PV_INVESTMENT_EUR: payback/pokriveno su None, ne puca."""
    d = client.get('/api/stats/roi').get_json()
    assert d['investicija_eur'] is None
    assert d['payback_godina'] is None
    assert d['pokriveno_perc'] is None
