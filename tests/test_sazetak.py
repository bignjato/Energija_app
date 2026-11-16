"""Model-svjesni sažeci: /api/stats/sazetak, /status, nova polja u /mjesecni,
is_bijeli, 'oba_izvora' u /usporedba, 'delta' u /dug, 'autarkija' u /optimalno."""

import os
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from hepapp import tariff
from hepapp.timeutil import TZ

BIJELI = 'HEPI bijeli — E-K-N-BIJ1'

SHEMA = '''
    CREATE TABLE ocitanja_satna (mjerno_mjesto TEXT, ts TEXT, kwh_plus REAL, kwh_minus REAL,
                                 kvarh_plus REAL, kvarh_minus REAL, UNIQUE(mjerno_mjesto, ts));
    CREATE TABLE ocitanja_15min (mjerno_mjesto TEXT, ts TEXT, kwh_plus REAL, kwh_minus REAL,
                                 UNIQUE(mjerno_mjesto, ts));
    CREATE TABLE ocitanja_dnevna (mjerno_mjesto TEXT, datum TEXT, kwh_plus REAL, kwh_minus REAL,
                                  UNIQUE(mjerno_mjesto, datum));
    CREATE TABLE hep_registri (mjerno_mjesto TEXT, datum TEXT, obis TEXT, value REAL,
                               UNIQUE(mjerno_mjesto, datum, obis));
    CREATE TABLE sma_dnevna (datum TEXT PRIMARY KEY, pv_generation_kwh REAL, feed_in_kwh REAL,
                             grid_consumption_kwh REAL, total_consumption_kwh REAL,
                             self_consumption_kwh REAL, autarky_rate REAL);
    CREATE TABLE sma_15min (ts TEXT PRIMARY KEY, pv_generation_wh REAL, feed_in_wh REAL,
                            grid_consumption_wh REAL, total_consumption_wh REAL,
                            pv_w_inv1 REAL DEFAULT 0, pv_w_inv2 REAL DEFAULT 0,
                            pv_w_total REAL DEFAULT 0);
    CREATE TABLE sma_live (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                           pv_generation_w REAL, feed_in_w REAL, external_consumption_w REAL,
                           total_consumption_w REAL, direct_consumption_w REAL,
                           autarky_rate REAL, self_consumption_rate REAL);
    CREATE TABLE sync_log (id INTEGER PRIMARY KEY AUTOINCREMENT,
                           ts TEXT DEFAULT (datetime('now')),
                           tip TEXT, status TEXT, poruka TEXT, zapisi INTEGER DEFAULT 0);
'''


def _mjeseci():
    """(tekući, prošli, pretprošli) kao 'YYYY-MM' + zadnji dani zadnja tri mjeseca."""
    danas = datetime.now().date()
    kraj1 = danas.replace(day=1) - timedelta(days=1)        # zadnji dan prošlog mj
    kraj2 = kraj1.replace(day=1) - timedelta(days=1)        # zadnji dan pretprošlog
    kraj3 = kraj2.replace(day=1) - timedelta(days=1)
    return danas, kraj1, kraj2, kraj3


@pytest.fixture
def baza(prijavljeni_klijent):
    """Prazna aplikacija (samo init_db) + funkcija za punjenje; vraća (client, sql)."""
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    tariff.invalidate_tarifa_cache()
    client = prijavljeni_klijent(path)

    def sql(skripta, *params):
        conn = sqlite3.connect(path)
        if params:
            conn.execute(skripta, params)
        else:
            conn.executescript(skripta)
        conn.commit(); conn.close()
        tariff.invalidate_tarifa_cache()

    yield client, sql
    tariff.invalidate_tarifa_cache()
    os.unlink(path)


def _napuni_mjerenja(sql):
    """Dnevna + satna očitanja za tekući mjesec (do danas) i dva prethodna."""
    danas, kraj1, kraj2, _ = _mjeseci()
    sql(SHEMA)
    d = kraj2.replace(day=1)
    while d <= danas:
        dan = d.isoformat()
        sql("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 12.0, 3.0)", dan)
        for h, kp, km in ((3, 4.0, 0.0), (12, 1.0, 3.0), (19, 7.0, 0.0)):
            sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, ?, ?, 0, 0)",
                f'{dan}T{h:02d}:00:00', kp, km)
        d += timedelta(days=1)


def _registri(sql, datum, vt, nt, pvt, pnt):
    for obis, v in (('A+_T1', vt), ('A+_T2', nt), ('A-_T1', pvt), ('A-_T2', pnt)):
        sql("INSERT INTO hep_registri VALUES ('OMM1', ?, ?, ?)", datum, obis, v)


def _racun(sql, period, iznos, model=BIJELI, dug=None, dospijece=None):
    sql("INSERT INTO racuni (period, iznos, model_tarife, dug, datum_racuna, datum_dospijeca) "
        "VALUES (?, ?, ?, ?, ?, ?)", period, iznos, model, dug, period + '-28', dospijece)


# ---------------------------------------------------------------- is_bijeli

def test_is_bijeli_redoslijed(baza):
    _, sql = baza
    assert tariff.is_bijeli() is False                      # nema računa ni configa
    sql("INSERT OR REPLACE INTO config (key, value) VALUES ('TARIFA_MODEL', 'bijeli')")
    assert tariff.is_bijeli() is True                       # config
    sql("DELETE FROM config WHERE key='TARIFA_MODEL'")
    _racun(sql, '2026-01', 10, model='Standardni')
    assert tariff.is_bijeli() is False
    _racun(sql, '2026-03', 10, model=BIJELI)                # najnoviji račun pobjeđuje
    _racun(sql, '2026-02', 10, model='Standardni')
    assert tariff.is_bijeli() is True
    _racun(sql, '2026-04', 10, model=None)                  # račun bez modela se preskače
    assert tariff.is_bijeli() is True
    assert tariff.get_model_tarife() == 'bijeli'


def test_is_bijeli_s_konekcijom_i_bez_tablice():
    conn = sqlite3.connect(':memory:')
    assert tariff.is_bijeli(conn) in (True, False)          # nema tablice racuni -> ne puca
    conn.execute('CREATE TABLE racuni (period TEXT, model_tarife TEXT)')
    conn.execute("INSERT INTO racuni VALUES ('02/2026', 'E-K-N-BIJ1')")   # stari zapis perioda
    conn.execute("INSERT INTO racuni VALUES ('2025-12', 'nešto drugo')")
    assert tariff.is_bijeli(conn) is True
    conn.execute('SELECT 1')                                # konekcija ostaje otvorena
    conn.close()


def test_efektivne_cijene_model_iz_racuna(baza):
    """/api/data više ne gleda samo config TARIFA_MODEL (koji nitko ne postavlja)."""
    client, sql = baza
    _napuni_mjerenja(sql)
    assert client.get('/api/data').get_json()['efektivne_cijene']['model'] == 'standardni'
    _racun(sql, '2026-01', 10)
    assert client.get('/api/data').get_json()['efektivne_cijene']['model'] == 'bijeli'


# ---------------------------------------------------------------- /mjesecni

def test_mjesecni_bijeli_registri_projekcija_satna(baza):
    client, sql = baza
    danas, kraj1, kraj2, _ = _mjeseci()
    _napuni_mjerenja(sql)
    prosli, pretprosli, tekuci = kraj1.isoformat()[:7], kraj2.isoformat()[:7], danas.isoformat()[:7]
    # delte prošlog mjeseca: uzeto 300 VT / 400 NT, predano 500 VT / 0 NT
    _registri(sql, kraj2.isoformat(), 1000, 2000, 5000, 10)
    _registri(sql, kraj1.isoformat(), 1300, 2400, 5500, 10)
    _racun(sql, prosli, 50.0)

    d = client.get('/api/stats/mjesecni').get_json()
    assert d['model'] == 'bijeli'
    po = {m['mjesec']: m for m in d['mjeseci']}

    m = po[prosli]
    ocek = tariff.izracunaj_racun_bijeli(300, 400, 500, 0, 30, datum=kraj1.isoformat())['iznos']
    otkup = tariff.otkup_viskova_bijeli(300, 400, 500, 0, datum=kraj1.isoformat())['ukupno_eur']
    assert m['model'] == 'bijeli' and m['procj_izvor'] == 'registri'
    assert m['procj_iznos'] == pytest.approx(ocek, abs=0.005) and ocek > 0
    assert m['otkup_eur'] == pytest.approx(otkup, abs=0.005) and otkup > 0
    assert m['stvarni_iznos'] == 50.0
    assert m['razlika'] == pytest.approx(ocek - 50.0, abs=0.006)
    assert 'procj_racun' in m and 'procj_trosak_neto' in m      # stara polja ostaju

    # tekući mjesec = ista projekcija kao /procjena-trenutni
    p = client.get('/api/stats/procjena-trenutni').get_json()
    t = po[tekuci]
    assert t['procj_izvor'] == 'projekcija'
    assert t['procj_iznos'] == p['racun_procjena']
    assert t['otkup_eur'] == p['otkup_procjena']['ukupno_eur']
    assert t['stvarni_iznos'] is None and t['razlika'] is None

    # mjesec bez registara: bijeli iz satnih očitanja (07–21 UTC+1 -> sat 12 i 19 su VT)
    s = po[pretprosli]
    n = kraj2.day
    ocek_s = tariff.izracunaj_racun_bijeli(8.0 * n, 4.0 * n, 3.0 * n, 0, 30,
                                           datum=pretprosli)['iznos']
    assert s['procj_izvor'] == 'satna'
    assert s['procj_iznos'] == pytest.approx(ocek_s, abs=0.01)

    # sažetak 12 mj: samo završeni mjeseci; uvezeni račun ima prednost pred procjenom
    saz = d['sazetak_12mj']
    assert saz['n_mjeseci'] == 2
    assert saz['racuni_eur'] == pytest.approx(50.0 + s['procj_iznos'], abs=0.01)
    assert saz['otkup_eur'] == pytest.approx(m['otkup_eur'] + s['otkup_eur'], abs=0.01)
    assert saz['neto_eur'] == pytest.approx(saz['racuni_eur'] - saz['otkup_eur'], abs=0.01)
    assert saz['najskuplji']['mjesec'] in (prosli, pretprosli)
    assert saz['najskuplji']['iznos'] == max(50.0, s['procj_iznos'])


def test_mjesecni_standardni_fallback(baza):
    client, sql = baza
    danas, kraj1, _, _ = _mjeseci()
    _napuni_mjerenja(sql)
    d = client.get('/api/stats/mjesecni').get_json()
    assert d['model'] == 'standardni'
    po = {m['mjesec']: m for m in d['mjeseci']}
    m = po[kraj1.isoformat()[:7]]
    assert m['model'] == 'standardni' and m['procj_izvor'] == 'standardni'
    assert m['procj_iznos'] == m['procj_racun']
    assert m['otkup_eur'] == 0 and m['stvarni_iznos'] is None and m['razlika'] is None
    assert po[danas.isoformat()[:7]]['procj_izvor'] == 'projekcija'


def test_mjesecni_djelomicni_registar_se_ne_koristi(baza):
    """Stanje brojila usred mjeseca nije mjesečni obračun -> ne smije biti 'registri'."""
    client, sql = baza
    _, kraj1, kraj2, _ = _mjeseci()
    _napuni_mjerenja(sql)
    _racun(sql, '2020-01', 1.0)
    _registri(sql, kraj1.replace(day=10).isoformat(), 1000, 2000, 0, 0)
    _registri(sql, kraj1.isoformat(), 1300, 2400, 0, 0)
    po = {m['mjesec']: m for m in client.get('/api/stats/mjesecni').get_json()['mjeseci']}
    assert po[kraj1.isoformat()[:7]]['procj_izvor'] == 'satna'


# ---------------------------------------------------------------- /sazetak

def test_sazetak_prazna_baza(baza):
    client, _ = baza
    r = client.get('/api/stats/sazetak')
    assert r.status_code == 200
    d = r.get_json()
    assert d['model'] == 'standardni' and d['model_naziv']
    assert d['racun'] is None and d['bez_solara'] is None
    assert d['mjesec']['ima_podataka'] is False
    assert d['godina']['n_mjeseci'] == 0 and d['godina']['racuni_eur'] == 0
    assert d['godina']['ukljucuje_procjenu_tekuceg'] is False
    assert d['zadnjih_12'] == {'racuni_eur': 0, 'otkup_eur': 0, 'neto_eur': 0, 'n_mjeseci': 0}
    # i ostali novi putevi na praznoj bazi
    assert client.get('/api/stats/status').status_code == 200
    m = client.get('/api/stats/mjesecni').get_json()
    assert m['mjeseci'] == [] and m['sazetak_12mj']['najskuplji'] is None
    assert client.get('/api/stats/procjena-trenutni').get_json()['ima_podataka'] is False


def test_sazetak_bijeli(baza):
    client, sql = baza
    danas, kraj1, kraj2, _ = _mjeseci()
    _napuni_mjerenja(sql)
    prosli = kraj1.isoformat()[:7]
    _registri(sql, kraj2.isoformat(), 1000, 2000, 5000, 10)
    _registri(sql, kraj1.isoformat(), 1300, 2400, 5500, 10)
    _racun(sql, kraj2.isoformat()[:7], 40.0, dug=300.0,
           dospijece=(danas - timedelta(days=20)).isoformat())
    _racun(sql, prosli, 50.0, dug=250.0, dospijece=(danas + timedelta(days=5)).isoformat())
    # PV prošlog mjeseca: 800 kWh -> samopotrošnja = 800 − 500 predano = 300 kWh
    sql("INSERT INTO sma_dnevna (datum, pv_generation_kwh) VALUES (?, 800)", kraj1.isoformat())

    d = client.get('/api/stats/sazetak').get_json()
    assert d['model'] == 'bijeli' and 'bijeli' in d['model_naziv']
    assert d['cijene_izvor'] == tariff.get_tarifa_izvor()
    assert d['racun'] == {'period': prosli, 'iznos': 50.0, 'dug': 250.0,
                          'dug_period': prosli,
                          'datum_dospijeca': (danas + timedelta(days=5)).isoformat(),
                          'dana_do_dospijeca': 5}

    # tekući mjesec = brojke iz /procjena-trenutni
    p = client.get('/api/stats/procjena-trenutni').get_json()
    mj = d['mjesec']
    assert mj['mjesec'] == p['mjesec'] and mj['ima_podataka'] is True
    assert mj['dani_s_podacima'] == p['dani_s_podacima']
    assert mj['n_dana_mjesec'] == p['n_dana_mjesec']
    assert mj['do_sada_eur'] == p['racun_proteklo']
    assert mj['procjena_eur'] == p['racun_procjena']
    assert mj['otkup_procjena_eur'] == p['otkup_procjena']['ukupno_eur']
    assert mj['neto_procjena_eur'] == pytest.approx(
        mj['procjena_eur'] - mj['otkup_procjena_eur'], abs=0.005)
    assert mj['vt_udio'] == p['vt_udio_stvarni']

    # godina do danas: uvezeni računi završenih mjeseci ove godine + projekcija tekućeg
    zatvoreni = [(k.isoformat()[:7], iznos) for k, iznos in ((kraj1, 50.0), (kraj2, 40.0))
                 if k.year == danas.year]
    g = d['godina']
    assert g['godina'] == danas.year and g['ukljucuje_procjenu_tekuceg'] is True
    assert g['n_mjeseci'] == len(zatvoreni) + 1
    assert g['racuni_eur'] == pytest.approx(
        sum(i for _, i in zatvoreni) + mj['procjena_eur'], abs=0.01)
    assert g['neto_eur'] == pytest.approx(g['racuni_eur'] - g['otkup_eur'], abs=0.01)

    z = d['zadnjih_12']
    assert z['n_mjeseci'] == 2 and z['racuni_eur'] == 90.0
    assert set(z) == {'racuni_eur', 'otkup_eur', 'neto_eur', 'n_mjeseci'}

    b = d['bez_solara']
    if kraj1.year != danas.year:            # siječanj: ove godine još nema završenog mjeseca
        assert b is None
        return
    ocek = tariff.izracunaj_racun_bijeli(300 + 300, 400, 0, 0, 30,
                                         datum=kraj1.isoformat())['iznos']
    otkup = tariff.otkup_viskova_bijeli(300, 400, 500, 0, datum=kraj1.isoformat())['ukupno_eur']
    assert b['n_mjeseci'] == 1                               # pretprošli nema PV podataka
    assert b['godina_eur'] == pytest.approx(ocek, abs=0.01)
    assert b['usteda_eur'] == pytest.approx(ocek - (50.0 - otkup), abs=0.02)
    assert b['usteda_eur'] > 0 and 'rocjena' in b['napomena']


def test_sazetak_dospijece_kasni_i_bez_pv(baza):
    client, sql = baza
    danas = datetime.now().date()
    _racun(sql, '2020-05', 10.0, dug=10.0, dospijece=(danas - timedelta(days=3)).isoformat())
    d = client.get('/api/stats/sazetak').get_json()
    assert d['racun']['dana_do_dospijeca'] == -3
    assert d['bez_solara'] is None


def test_sazetak_dug_s_racuna_koji_ga_ima(baza):
    """Ručno uneseni najnoviji račun (bez duga/dospijeća) ne skriva poznati dug:
    /sazetak i /dug pokazuju isti dug, s istog računa."""
    client, sql = baza
    sql(SHEMA)
    danas = datetime.now().date()
    dosp = (danas + timedelta(days=5)).isoformat()
    _racun(sql, '2020-04', 40.0, dug=300.0, dospijece='2020-05-20')
    _racun(sql, '2020-05', 50.0, dug=223.29, dospijece=dosp)
    # ručni unos: samo period + iznos (kao addRacun bez PDF-a)
    sql("INSERT INTO racuni (period, iznos) VALUES ('2020-06', 63.39)")

    r = client.get('/api/stats/sazetak').get_json()['racun']
    assert (r['period'], r['iznos']) == ('2020-06', 63.39)       # zadnji račun
    assert (r['dug'], r['dug_period']) == (223.29, '2020-05')    # zadnji poznati dug
    assert r['datum_dospijeca'] == dosp and r['dana_do_dospijeca'] == 5

    t = client.get('/api/stats/dug').get_json()['trenutni']
    assert (t['period'], t['dug']) == (r['dug_period'], r['dug'])

    # najnoviji račun ima dug -> dug_period == period
    sql("UPDATE racuni SET dug = 100.0 WHERE period = '2020-06'")
    r = client.get('/api/stats/sazetak').get_json()['racun']
    assert (r['dug'], r['dug_period'], r['datum_dospijeca']) == (100.0, '2020-06', None)

    # nijedan račun nema dug -> dug i dug_period null
    sql("UPDATE racuni SET dug = NULL")
    r = client.get('/api/stats/sazetak').get_json()['racun']
    assert r['period'] == '2020-06' and r['dug'] is None and r['dug_period'] is None


def test_zadnji_racun_i_dug_stari_zapis_perioda(baza):
    """'MM/YYYY' se sortira normalizirano i u /dug i u /sazetak."""
    client, sql = baza
    sql(SHEMA)
    _racun(sql, '2020-12', 10.0, dug=500.0)
    sql("INSERT INTO racuni (period, iznos, dug) VALUES ('02/2021', 20.0, 400.0)")
    d = client.get('/api/stats/dug').get_json()
    assert [x['period'] for x in d['racuni']] == ['2020-12', '02/2021']
    assert d['trenutni']['dug'] == 400.0 and d['racuni'][1]['delta'] == -100.0
    r = client.get('/api/stats/sazetak').get_json()['racun']
    assert (r['period'], r['dug'], r['dug_period']) == ('2021-02', 400.0, '2021-02')


# ---------------------------------------------------------------- /status

def test_status_svjeze_i_zastarjelo(baza):
    client, sql = baza
    sql(SHEMA)
    sada = datetime.now()
    utc = datetime.now(timezone.utc)
    uzorak = (sada - timedelta(hours=10)).replace(minute=0, second=0, microsecond=0)
    sql("INSERT INTO ocitanja_15min VALUES ('OMM1', ?, 0.5, 0)", uzorak.strftime('%Y-%m-%dT%H:%M:%S'))
    # noviji prazan red (bez mjerenja) ne smije se računati kao stvarni uzorak
    sql("INSERT INTO ocitanja_15min VALUES ('OMM1', ?, 0, 0)",
        (sada - timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M:%S'))
    sma_ts = utc - timedelta(minutes=3)
    sql("INSERT INTO sma_live (ts, pv_generation_w) VALUES (?, 100)",
        sma_ts.strftime('%Y-%m-%dT%H:%M:%S+00:00'))
    sql("INSERT INTO sync_log (ts, tip, status) VALUES (datetime('now','-2 hours'), 'sync', 'OK')")
    sql("INSERT INTO sync_log (ts, tip, status) VALUES (datetime('now','-3 hours'), 'login', 'ERROR')")
    sql("INSERT INTO sync_log (ts, tip, status) VALUES (datetime('now','-3 days'), 'login', 'ERROR')")

    d = client.get('/api/stats/status').get_json()
    assert d['hep']['zadnji_uzorak'] == uzorak.strftime('%Y-%m-%dT%H:%M:%S')
    assert d['hep']['ok'] is True and 10 <= d['hep']['kasni_h'] <= 11.1
    assert d['hep']['zadnji_dan'] == (uzorak - timedelta(minutes=15)).strftime('%Y-%m-%d')
    # SMA ts je u bazi UTC -> u odgovoru lokalno vrijeme
    assert d['sma']['zadnji'] == sma_ts.astimezone(TZ).strftime('%Y-%m-%dT%H:%M:%S')
    assert d['sma']['ok'] is True and d['sma']['kasni_min'] in (3, 4)
    ok_lok = (utc - timedelta(hours=2)).astimezone(TZ)
    assert d['sync']['zadnji_ok'][:13] == ok_lok.strftime('%Y-%m-%dT%H')
    assert d['sync']['greske_24h'] == 1
    assert d['model'] == 'standardni' and d['cijene_izvor'] == tariff.get_tarifa_izvor()

    # zastarjelo: HEP > 48 h, SMA > 15 min
    sql("DELETE FROM ocitanja_15min WHERE kwh_plus > 0")
    sql("INSERT INTO ocitanja_15min VALUES ('OMM1', ?, 0.5, 0)",
        (sada - timedelta(hours=60)).strftime('%Y-%m-%dT%H:%M:%S'))
    sql("INSERT INTO sma_live (ts, pv_generation_w) VALUES (?, 100)",
        (utc - timedelta(minutes=40)).strftime('%Y-%m-%dT%H:%M:%S+00:00'))
    d = client.get('/api/stats/status').get_json()
    assert d['hep']['ok'] is False and d['hep']['kasni_h'] >= 59
    assert d['sma']['ok'] is False and d['sma']['kasni_min'] >= 40


def test_status_prazno(baza):
    client, _ = baza
    d = client.get('/api/stats/status').get_json()
    assert d['hep'] == {'zadnji_uzorak': None, 'zadnji_dan': None, 'kasni_h': None, 'ok': False}
    assert d['sma'] == {'zadnji': None, 'kasni_min': None, 'ok': False}
    assert d['sync'] == {'zadnji_ok': None, 'greske_24h': 0}


# ---------------------------------------------------------------- /usporedba

def test_usporedba_oba_izvora_i_null(baza):
    client, sql = baza
    sql(SHEMA)
    danas = datetime.now().date()
    d1, d2, d3 = ((danas - timedelta(days=n)).isoformat() for n in (1, 2, 3))
    # d1: oba izvora; d2: samo HEP; d3: SMA samo s PV kolonom (history import)
    sql("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 10, 2)", d1)
    sql("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 11, 3)", d2)
    sql("INSERT INTO ocitanja_dnevna VALUES ('OMM1', ?, 12, 4)", d3)
    sql("INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, grid_consumption_kwh) "
        "VALUES (?, 20, 2.1, 9.8)", d1)
    sql("INSERT INTO sma_dnevna (datum, pv_generation_kwh) VALUES (?, 30)", d3)
    # danas: SMA ima, HEP još nema
    sql("INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, grid_consumption_kwh) "
        "VALUES (?, 5, 1, 4)", danas.isoformat())

    po = {r['datum']: r for r in client.get('/api/stats/usporedba').get_json()}
    assert po[d1]['oba_izvora'] is True
    assert po[d2]['oba_izvora'] is False
    assert po[d2]['sma_mreza'] is None and po[d2]['sma_predaja'] is None
    assert po[d2]['sma_proizvodnja'] is None
    assert po[d3]['oba_izvora'] is False and po[d3]['sma_mreza'] is None
    dan = po[danas.isoformat()]
    assert dan['oba_izvora'] is False                 # današnji dan nije završen
    assert dan['hep_potrosnja'] is None and dan['hep_predaja'] is None   # null, ne 0

    # i kad HEP danas ima satne podatke, današnji dan ostaje neusporediv
    sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, 1.5, 0.2, 0, 0)",
        danas.isoformat() + 'T00:00:00')
    dan = {r['datum']: r for r in client.get('/api/stats/usporedba').get_json()}[danas.isoformat()]
    assert dan['hep_potrosnja'] == 1.5 and dan['oba_izvora'] is False


# ---------------------------------------------------------------- /dug

def test_dug_delta(baza):
    client, sql = baza
    sql(SHEMA)
    _racun(sql, '2026-03', 30.0, dug=400.0)
    _racun(sql, '2026-01', 10.0, dug=500.0)
    _racun(sql, '2026-02', 20.0, dug=550.5)
    rac = client.get('/api/stats/dug').get_json()['racuni']
    assert [r['period'] for r in rac] == ['2026-01', '2026-02', '2026-03']   # kronološki
    assert [r['delta'] for r in rac] == [None, 50.5, -150.5]


# ---------------------------------------------------------------- /optimalno

def _sma_15min(sql, dan, sat_lokalno, w):
    """4 uzorka od `w` W u zadanom LOKALNOM satu (ts u bazi je UTC sa 'Z')."""
    for minuta in (0, 15, 30, 45):
        lok = datetime.strptime(f'{dan}T{sat_lokalno:02d}:{minuta:02d}', '%Y-%m-%dT%H:%M')
        utc = lok.replace(tzinfo=TZ).astimezone(timezone.utc)
        sql("INSERT INTO sma_15min (ts, pv_w_total) VALUES (?, ?)",
            utc.strftime('%Y-%m-%dT%H:%M:%SZ'), w)


def test_optimalno_autarkija_zajednicko_razdoblje(baza):
    client, sql = baza
    sql(SHEMA)
    danas = datetime.now().date()
    jucer = (danas - timedelta(days=1)).isoformat()
    davno = (danas - timedelta(days=40)).isoformat()
    # jučer (oba izvora): podne uzeto 1, predano 0, PV 3 kW -> kuća 4 kWh, autarkija 75 %
    sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, 1.0, 0, 0, 0)", jucer + 'T12:00:00')
    sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, 0.5, 2.0, 0, 0)", jucer + 'T13:00:00')
    sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, 2.0, 0, 0, 0)", jucer + 'T02:00:00')
    _sma_15min(sql, jucer, 12, 3000)
    _sma_15min(sql, jucer, 13, 4000)     # uzeto 0.5 + PV 4 − predano 2 = 2.5 -> 80 %
    _sma_15min(sql, jucer, 2, 0)         # noć: sve iz mreže -> 0 %
    # prije 40 dana samo HEP (SMA tada ne postoji) -> ne smije ući u autarkiju
    sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, 5.0, 0, 0, 0)", davno + 'T12:00:00')
    sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, 5.0, 0, 0, 0)", davno + 'T18:00:00')

    d = client.get('/api/stats/optimalno').get_json()
    po = {r['sat']: r for r in d['hep_satno']}
    assert d['autarkija_razdoblje'] == {'od': jucer, 'do': jucer}
    assert po[12]['prosj_potrosnja'] == 3.0          # stari prosjek i dalje preko svega
    assert po[12]['autarkija'] == 75.0
    assert po[13]['autarkija'] == 80.0
    assert po[2]['autarkija'] == 0.0
    assert po[18]['autarkija'] is None               # sat bez podataka u zajedničkom razdoblju
    assert all(r['autarkija'] is None or 0 <= r['autarkija'] <= 100 for r in d['hep_satno'])


def test_optimalno_autarkija_bez_sma(baza):
    client, sql = baza
    sql('''CREATE TABLE ocitanja_satna (mjerno_mjesto TEXT, ts TEXT, kwh_plus REAL,
                                        kwh_minus REAL, kvarh_plus REAL, kvarh_minus REAL);''')
    jucer = (datetime.now().date() - timedelta(days=1)).isoformat()
    sql("INSERT INTO ocitanja_satna VALUES ('OMM1', ?, 1.0, 0, 0, 0)", jucer + 'T12:00:00')
    d = client.get('/api/stats/optimalno').get_json()
    assert d['autarkija_razdoblje'] is None
    assert [r['autarkija'] for r in d['hep_satno']] == [None]
