"""Smoke + logika testovi za /api/stats/* endpointe."""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta

import pytest


@pytest.fixture
def client(prijavljeni_klijent):
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

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

    yield prijavljeni_klijent(path)
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
def client_dug(prijavljeni_klijent):
    """Kao `client`, ali s 5 računa u silaznom trendu duga (−100 €/mj)."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

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

    yield prijavljeni_klijent(path)
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
def client_pv(prijavljeni_klijent):
    """Registri s A+/A- za net-metering saldo + ROI, uz investiciju u config."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)

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

    yield prijavljeni_klijent(path)
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


# ===== vremenske zone, VT po UTC satu, peak-snaga, cijene =====

from zoneinfo import ZoneInfo

from hepapp.timeutil import now_local

_ZG = ZoneInfo('Europe/Zagreb')
_UTC = ZoneInfo('UTC')
_F = '%Y-%m-%dT%H:%M:%S'


def _lokalno(delta):
    """Naivni lokalni 'T' ts pomaknut za delta od sada (HEP format)."""
    return (now_local().replace(tzinfo=None) + delta).strftime(_F)


def _je_vt(ts):
    """Neovisna referenca: VT = 06:00-20:00 UTC (ts = lokalni početak sata)."""
    utc_h = datetime.strptime(ts, _F).replace(tzinfo=_ZG).astimezone(_UTC).hour
    return 6 <= utc_h <= 19


@pytest.fixture
def baza(prijavljeni_klijent):
    """Prazna shema; test sam puni podatke preko `baza.sql(...)`."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE config (key TEXT PRIMARY KEY, value TEXT, updated TEXT);
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
        CREATE TABLE sma_15min (ts TEXT PRIMARY KEY, pv_generation_wh REAL, feed_in_wh REAL,
                                grid_consumption_wh REAL, total_consumption_wh REAL,
                                pv_w_inv1 REAL, pv_w_inv2 REAL, pv_w_total REAL);
    ''')
    conn.commit(); conn.close()

    class _Baza:
        client = prijavljeni_klijent(path)

        @staticmethod
        def sql(stmt, rows):
            c = sqlite3.connect(path)
            c.executemany(stmt, rows)
            c.commit(); c.close()

        @staticmethod
        def satna(rows):
            _Baza.sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, ?, ?, 0, 0)", rows)

        @staticmethod
        def min15(rows):
            _Baza.sql("INSERT INTO ocitanja_15min VALUES ('OMM1', ?, ?, ?)", rows)

    yield _Baza
    os.unlink(path)


def test_vt_kalibracija_ljeti_08_22(baza):
    """Srpanj: 07h je NT, 21h je VT (ljetno računanje); vikend ne ulazi u workdays."""
    baza.satna([
        ('2026-07-15T07:00:00', 1.0, 0),    # srijeda, NT (po starom bi bio VT)
        ('2026-07-15T08:00:00', 2.0, 0),    # VT
        ('2026-07-15T21:00:00', 4.0, 0),    # VT (po starom bi bio NT)
        ('2026-07-15T22:00:00', 8.0, 0),    # NT
        ('2026-07-18T10:00:00', 16.0, 0),   # subota, VT samo u "simple"
        ('2026-08-01T00:00:00', 99.0, 0),   # idući mjesec — ne ulazi
        ('2026-06-30T23:00:00', 99.0, 0),   # prethodni mjesec — ne ulazi
    ])
    d = baza.client.get('/api/stats/vt-kalibracija?mjesec=2026-07').get_json()
    assert len(d['mjeseci']) == 1
    m = d['mjeseci'][0]
    assert m['mjesec'] == '2026-07'
    assert m['ukupno_kwh'] == 31.0
    assert m['vt_simple_kwh'] == 22.0      # 2 + 4 + 16
    assert m['nt_simple_kwh'] == 9.0       # 1 + 8
    assert m['vt_workdays_kwh'] == 6.0     # 2 + 4


def test_vt_kalibracija_zimi_07_21(baza):
    baza.satna([
        ('2026-01-14T06:00:00', 1.0, 0),    # NT
        ('2026-01-14T07:00:00', 2.0, 0),    # VT
        ('2026-01-14T20:00:00', 4.0, 0),    # VT
        ('2026-01-14T21:00:00', 8.0, 0),    # NT
    ])
    m = baza.client.get('/api/stats/vt-kalibracija?mjesec=2026-01').get_json()['mjeseci'][0]
    assert m['vt_simple_kwh'] == 6.0
    assert m['nt_simple_kwh'] == 9.0


def test_vt_kalibracija_neispravan_mjesec_ne_puca(baza):
    r = baza.client.get('/api/stats/vt-kalibracija?mjesec=nije')
    assert r.status_code == 200


def test_procjena_trenutni_vt_po_utc_satu(baza):
    """Tekući mjesec: VT/NT podjela prati UTC sat (zimi 07-21, ljeti 08-22)."""
    mj = now_local().strftime('%Y-%m')
    rows = [(f'{mj}-01T{h:02d}:00:00', float(2 ** i), float(2 ** i) / 10)
            for i, h in enumerate((6, 7, 8, 20, 21, 22))]
    baza.satna(rows)
    # susjedni mjeseci ne smiju ući u zbroj (raspon ts umjesto substr)
    prosli = (now_local().replace(day=1) - timedelta(days=1)).strftime('%Y-%m-%d')
    baza.satna([(f'{prosli}T23:00:00', 500.0, 50.0)])

    d = baza.client.get('/api/stats/procjena-trenutni').get_json()
    assert d['mjesec'] == mj
    vt = round(sum(kp for ts, kp, _ in rows if _je_vt(ts)), 2)
    nt = round(sum(kp for ts, kp, _ in rows if not _je_vt(ts)), 2)
    assert d['kwh_plus_proteklo'] == 63.0
    assert d['kwh_vt_proteklo'] == vt
    assert d['kwh_nt_proteklo'] == nt
    assert d['kwh_vt_pred_proteklo'] == round(sum(km for ts, _, km in rows if _je_vt(ts)), 2)
    # sat 08 i 20 su VT cijele godine; 06 i 22 uvijek NT
    assert vt >= 4.0 + 8.0 and nt >= 1.0 + 32.0
    assert d['tarifa']['vt_kwh'] > 0 and d['tarifa']['pdv'] > 0


def test_vt_nt_trosak_po_utc_satu(baza):
    mj = now_local().strftime('%Y-%m')
    rows = [(f'{mj}-01T{h:02d}:00:00', float(2 ** i), 0.0)
            for i, h in enumerate((6, 7, 8, 20, 21, 22))]
    # samo redovi koji nisu u budućnosti-neovisno: endpoint nema gornju granicu
    baza.satna(rows)
    d = baza.client.get('/api/stats/vt-nt-trosak').get_json()
    m = next(x for x in d['mjeseci'] if x['mjesec'] == mj)
    assert m['vt_kwh'] == round(sum(kp for ts, kp, _ in rows if _je_vt(ts)), 2)
    assert m['nt_kwh'] == round(sum(kp for ts, kp, _ in rows if not _je_vt(ts)), 2)
    assert d['tarife']['vt_kwh_eur'] > 0


def test_optimalno_ukljucuje_sate_samo_s_predajom(baza):
    """Sat u kojem kuća samo predaje (kwh_plus = 0) mora ući u prosjek."""
    baza.satna([
        ('2026-07-15T12:00:00', 0.0, 2.0),    # samo predaja
        ('2026-07-16T12:00:00', 1.0, 0.0),
        ('2026-07-15T03:00:00', 0.0, 0.0),    # prazan red — i dalje se preskače
        (_lokalno(timedelta(days=2)), 7.0, 7.0),   # budućnost — ne ulazi
    ])
    d = baza.client.get('/api/stats/optimalno').get_json()
    po_satu = {r['sat']: r for r in d['hep_satno']}
    assert po_satu[12]['n'] == 2
    assert po_satu[12]['prosj_potrosnja'] == 0.5
    assert po_satu[12]['prosj_predaja'] == 1.0
    assert 3 not in po_satu
    assert sum(r['n'] for r in d['hep_satno']) == 2


def test_optimalno_danasnji_red_ulazi(baza):
    """Regresija: red s današnjim datumom nije "u budućnosti"."""
    ts = _lokalno(timedelta(minutes=-5))
    baza.satna([(ts, 1.0, 0.0)])
    d = baza.client.get('/api/stats/optimalno').get_json()
    assert sum(r['n'] for r in d['hep_satno']) == 1


def test_optimalno_sma_sat_je_lokalni(baza):
    # 10:00 UTC u srpnju = 12h lokalno
    baza.sql("INSERT INTO sma_15min (ts, pv_w_total) VALUES (?, ?)",
             [('2026-07-15T10:00:00Z', 4000.0)])
    d = baza.client.get('/api/stats/optimalno').get_json()
    assert [r['sat'] for r in d['sma_satno']] == [12]


def _peak_referenca(path_rows, sada):
    """Stara logika /peak-snaga (po jedan upit po razdoblju), nad listom redova."""
    out = {}
    for label, dana in [('dan', 1), ('tjedan', 7), ('mjesec', 30), ('godina', 365), ('uvijek', None)]:
        od = (sada - timedelta(days=dana)).strftime(_F) if dana else ''
        do = sada.strftime(_F)
        u = [r for r in path_rows if od <= r[0] <= do]
        p = max(u, key=lambda r: r[1], default=None)
        pr = max((r for r in u if r[2] > 0), key=lambda r: r[2], default=None)
        out[label] = {
            'peak_potrosnja_kw': round(p[1] * 4, 2) if p else None,
            'peak_potrosnja_ts': p[0] if p else None,
            'peak_predaja_kw':   round(pr[2] * 4, 2) if pr else None,
            'peak_predaja_ts':   pr[0] if pr else None,
        }
    return out


def test_peak_snaga_iste_vrijednosti_kao_prije(baza):
    """Sažeti upit (2 prolaza) daje isto što i stari upiti po razdoblju."""
    rows = [
        (_lokalno(timedelta(hours=-2)),   0.30, 0.20),
        (_lokalno(timedelta(hours=-5)),   0.25, 0.00),
        (_lokalno(timedelta(days=-3)),    0.90, 0.00),
        (_lokalno(timedelta(days=-20)),   0.70, 0.80),
        (_lokalno(timedelta(days=-200)),  1.10, 0.60),
        (_lokalno(timedelta(days=-500)),  1.50, 1.25),
        (_lokalno(timedelta(hours=3)),    9.90, 9.90),   # budućnost — ne ulazi
    ]
    baza.min15(rows)
    sada = now_local().replace(tzinfo=None)
    d = baza.client.get('/api/stats/peak-snaga').get_json()

    assert d == _peak_referenca(rows, sada)
    # i eksplicitno, da test ne ovisi samo o referenci
    assert d['dan']    == {'peak_potrosnja_kw': 1.2, 'peak_potrosnja_ts': rows[0][0],
                           'peak_predaja_kw': 0.8,  'peak_predaja_ts': rows[0][0]}
    assert d['tjedan']['peak_potrosnja_kw'] == 3.6 and d['tjedan']['peak_potrosnja_ts'] == rows[2][0]
    assert d['tjedan']['peak_predaja_kw'] == 0.8
    assert d['mjesec']['peak_potrosnja_kw'] == 3.6
    assert d['mjesec']['peak_predaja_kw'] == 3.2 and d['mjesec']['peak_predaja_ts'] == rows[3][0]
    assert d['godina']['peak_potrosnja_kw'] == 4.4 and d['godina']['peak_predaja_kw'] == 3.2
    assert d['uvijek']['peak_potrosnja_kw'] == 6.0 and d['uvijek']['peak_potrosnja_ts'] == rows[5][0]
    assert d['uvijek']['peak_predaja_kw'] == 5.0


def test_peak_snaga_prazno_i_bez_predaje(baza):
    d = baza.client.get('/api/stats/peak-snaga').get_json()
    assert d['dan'] == d['uvijek'] == {
        'peak_potrosnja_kw': None, 'peak_potrosnja_ts': None,
        'peak_predaja_kw': None, 'peak_predaja_ts': None}
    # samo stari red, bez predaje: uža razdoblja prazna, šira ga vide
    ts = _lokalno(timedelta(days=-10))
    baza.min15([(ts, 0.5, 0.0)])
    d = baza.client.get('/api/stats/peak-snaga').get_json()
    assert d['dan']['peak_potrosnja_kw'] is None and d['tjedan']['peak_potrosnja_kw'] is None
    assert d['mjesec']['peak_potrosnja_kw'] == 2.0 and d['mjesec']['peak_potrosnja_ts'] == ts
    assert d['uvijek']['peak_predaja_kw'] is None


def test_usporedba_danas_iz_satne_i_sma_utc(baza):
    """"Danas" je LOKALNI dan: HEP satni redovi od danas + SMA redovi čiji UTC ts
    pada u lokalni dan (lokalnih 00:30 je još jučerašnji UTC datum)."""
    danas = now_local().strftime('%Y-%m-%d')
    jucer = (now_local() - timedelta(days=1)).strftime('%Y-%m-%d')
    baza.satna([
        (f'{danas}T00:00:00', 1.0, 0.25),
        (f'{danas}T01:00:00', 2.0, 0.5),
        (f'{jucer}T23:00:00', 50.0, 50.0),       # jučer — ne ulazi
    ])

    def utc_z(dan, hhmm):
        lok = datetime.strptime(f'{dan}T{hhmm}:00', _F).replace(tzinfo=_ZG)
        return lok.astimezone(_UTC).strftime('%Y-%m-%dT%H:%M:%SZ')

    baza.sql("INSERT INTO sma_15min (ts, pv_generation_wh, feed_in_wh, grid_consumption_wh, "
             "total_consumption_wh) VALUES (?, ?, ?, ?, ?)", [
        (utc_z(danas, '00:30'), 1000.0, 200.0, 300.0, 600.0),   # UTC datum = jučer
        (utc_z(jucer, '23:30'), 9000.0, 9000.0, 9000.0, 9000.0),  # lokalno jučer — ne ulazi
    ])
    d = baza.client.get('/api/stats/usporedba').get_json()
    red = next(r for r in d if r['datum'] == danas)
    assert red['_realtime'] is True
    assert red['hep_potrosnja'] == 3.0 and red['hep_predaja'] == 0.75
    assert red['sma_proizvodnja'] == 1.0
    assert red['sma_mreza'] == 0.3 and red['sma_potrosnja'] == 0.6
    assert red['autarky_rate'] == 0.5


def test_mjesecni_tarifa_je_pravi_dict(client):
    d = client.get('/api/stats/mjesecni').get_json()
    assert isinstance(d['tarifa'], dict)
    assert d['tarifa']['vt_opskrba'] > 0 and 'pdv' in d['tarifa']
    # današnji dnevni red (lokalni datum) je u tekućem mjesecu
    assert d['mjeseci'][0]['mjesec'] == now_local().strftime('%Y-%m')


def test_cijene_polja_i_izvor(client, monkeypatch):
    import hepapp.tariff as tariff
    d = client.get('/api/stats/cijene').get_json()
    assert isinstance(d['izvor'], str) and d['izvor']
    for k in ('pretplata_ukupno', 'vt_opskrba', 'nt_opskrba', 'pdv', 'otkup',
              'vt_udio_perc', 'vt_full_eur_kwh', 'nt_full_eur_kwh', 'avg_full_eur_kwh'):
        assert k in d
    assert d['vt_full_eur_kwh'] > d['nt_full_eur_kwh'] > 0

    # izvor dolazi iz tariff.get_tarifa_izvor() kad postoji ...
    monkeypatch.setattr(tariff, 'get_tarifa_izvor', lambda datum=None: 'račun TEST', raising=False)
    assert client.get('/api/stats/cijene').get_json()['izvor'] == 'račun TEST'
    # ... a bez nje (starija tariff.py) ostaje stari opis
    monkeypatch.delattr(tariff, 'get_tarifa_izvor')
    assert client.get('/api/stats/cijene').get_json()['izvor'] == 'default HEP_TARIFA'


def test_weather_greska_ne_curi_klijentu(client, monkeypatch):
    import requests

    def _pukni(*a, **k):
        raise RuntimeError('interna-tajna-poruka http://10.0.0.5:8123')
    monkeypatch.setattr(requests, 'get', _pukni)
    r = client.get('/api/stats/weather')
    assert r.status_code == 502
    assert 'error' in r.get_json()
    assert 'tajna' not in r.get_data(as_text=True) and '10.0.0.5' not in r.get_data(as_text=True)


def test_power_limit_history_greska_ne_curi(client, monkeypatch):
    import requests
    conn = sqlite3.connect(__import__('hepapp.db', fromlist=['DB_PATH']).DB_PATH)
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('HA_ENT_POWER_LIMIT','sensor.limit')")
    conn.commit(); conn.close()
    monkeypatch.setenv('HA_URL', 'http://ha.invalid:8123')
    monkeypatch.setenv('HA_TOKEN', 'tok')

    def _pukni(*a, **k):
        raise RuntimeError('interna-tajna-poruka')
    monkeypatch.setattr(requests, 'get', _pukni)
    d = client.get('/api/stats/power-limit-history').get_json()
    assert d['series'] == [] and 'tajna' not in d['note']
