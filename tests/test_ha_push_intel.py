"""Testovi ha_push_intel: collect() nad tmp bazom + push() s lažnim HA klijentom."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import ha_push_intel
from hepapp import tariff

SADA = datetime(2026, 7, 14, 23, 30, tzinfo=timezone.utc)   # = 15.7. 01:30 lokalno

SCHEMA = '''
    CREATE TABLE sma_live (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
        pv_generation_w REAL, feed_in_w REAL, external_consumption_w REAL,
        total_consumption_w REAL, direct_consumption_w REAL,
        autarky_rate REAL, self_consumption_rate REAL, battery_soc REAL
    );
    CREATE TABLE sma_dnevna (
        datum TEXT PRIMARY KEY,
        pv_generation_kwh REAL, feed_in_kwh REAL, grid_consumption_kwh REAL,
        total_consumption_kwh REAL, self_consumption_kwh REAL, autarky_rate REAL,
        pv_kwh_inv1 REAL, pv_kwh_inv2 REAL
    );
    CREATE TABLE racuni (id INTEGER PRIMARY KEY AUTOINCREMENT, period TEXT UNIQUE,
                         iznos REAL, dug REAL);
    CREATE TABLE ocitanja_dnevna (
        id INTEGER PRIMARY KEY AUTOINCREMENT, mjerno_mjesto TEXT, datum TEXT,
        kwh_plus REAL, kwh_minus REAL
    );
    CREATE TABLE hep_registri (
        id INTEGER PRIMARY KEY AUTOINCREMENT, mjerno_mjesto TEXT, datum TEXT,
        obis TEXT, value REAL
    );
'''


class FakeHA:
    def __init__(self):
        self.states = {}

    def set_state(self, entity_id, state, attributes=None):
        self.states[entity_id] = (state, dict(attributes or {}))
        return True


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = sqlite3.connect(str(tmp_path / 'intel.db'))
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    monkeypatch.setattr(ha_push_intel, '_now_utc', lambda: SADA)
    monkeypatch.setattr(ha_push_intel, 'VISAK_PRAG_W', 500.0)
    yield c
    c.close()


def _live(conn, minuta_staro, feed):
    ts = (SADA - timedelta(minutes=minuta_staro)).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    conn.execute('INSERT INTO sma_live (ts, pv_generation_w, feed_in_w) VALUES (?,?,?)',
                 (ts, 3000.0, feed))
    conn.commit()


def _push(conn):
    ha = FakeHA()
    vals = ha_push_intel.collect(conn)
    ha_push_intel.push(ha, vals)
    return vals, ha.states


def test_svjez_visak_iznad_praga(conn):
    _live(conn, 4, 1234.0)
    vals, st = _push(conn)
    assert vals['visak_w'] == 1234.0 and vals['isplati'] is True
    assert st['sensor.energija_solarni_visak'][0] == 1234.0
    assert st['binary_sensor.energija_isplati_se_trositi'][0] == 'on'


def test_svjez_visak_ispod_praga(conn):
    _live(conn, 4, 120.0)
    vals, st = _push(conn)
    assert vals['isplati'] is False
    assert st['binary_sensor.energija_isplati_se_trositi'][0] == 'off'


def test_zastario_sma_live_gura_unavailable(conn):
    _live(conn, 45, 2500.0)     # visok višak, ali 45 min star
    vals, st = _push(conn)
    assert vals['visak_w'] is None and vals['isplati'] is None
    stanje, attrs = st['sensor.energija_solarni_visak']
    assert stanje == 'unavailable'
    # isti atributi kao kod normalnog pusha
    assert attrs == {'unit_of_measurement': 'W', 'friendly_name': 'Solarni višak',
                     'icon': 'mdi:solar-power', 'device_class': 'power'}
    stanje, attrs = st['binary_sensor.energija_isplati_se_trositi']
    assert stanje == 'unavailable'      # ne 'off' iz starih podataka
    assert attrs['friendly_name'] == 'Isplati se trošiti'
    assert attrs['visak_prag_w'] == 500.0


def test_prazan_sma_live_gura_unavailable(conn):
    _, st = _push(conn)
    assert st['sensor.energija_solarni_visak'][0] == 'unavailable'
    assert st['binary_sensor.energija_isplati_se_trositi'][0] == 'unavailable'


def test_feed_null_nije_nula(conn):
    _live(conn, 2, None)        # svjež red, ali feed senzor nedostupan
    vals, st = _push(conn)
    assert vals['visak_w'] is None
    assert st['sensor.energija_solarni_visak'][0] == 'unavailable'
    assert st['binary_sensor.energija_isplati_se_trositi'][0] == 'unavailable'


def test_ts_bez_offseta_je_zastario(conn):
    conn.execute("INSERT INTO sma_live (ts, feed_in_w) VALUES ('2026-07-14T23:29:00', 900)")
    conn.commit()
    vals, _ = _push(conn)
    assert vals['visak_w'] is None


def test_dnevni_podaci_po_lokalnom_danu(conn):
    # SADA je 14.7. 23:30 UTC = 15.7. lokalno -> čita se red 15.7.
    conn.execute("INSERT INTO sma_dnevna (datum, feed_in_kwh, grid_consumption_kwh, "
                 "total_consumption_kwh, pv_kwh_inv1, pv_kwh_inv2) "
                 "VALUES ('2026-07-14', 14.0, 6.0, 23.0, 9.0, 8.0)")
    conn.execute("INSERT INTO sma_dnevna (datum, feed_in_kwh, grid_consumption_kwh, "
                 "total_consumption_kwh) VALUES ('2026-07-15', 0.0, 0.5, 2.0)")
    conn.execute("INSERT INTO racuni (period, iznos, dug) VALUES ('2026-06', 60.0, 12.34)")
    conn.execute("INSERT INTO ocitanja_dnevna (mjerno_mjesto, datum, kwh_plus, kwh_minus) "
                 "VALUES ('M1', '2026-07-01', 10.0, 4.0)")
    conn.commit()
    vals, st = _push(conn)

    t = tariff.get_tarifa()
    buy_vt = (t['vt_opskrba'] + t['vt_distrib'] + t['vt_prijenos'] + t['solidarna'] + t['oie']) * (1 + t['pdv'])
    buy_nt = (t['nt_opskrba'] + t['nt_distrib'] + t['nt_prijenos'] + t['solidarna'] + t['oie']) * (1 + t['pdv'])
    assert vals['vt_udio'] == 45.0      # nema registara -> default
    assert vals['trosak_danas'] == round(0.5 * (0.45 * buy_vt + 0.55 * buy_nt), 2)
    assert vals['autarkija_danas'] == 75.0
    assert vals['dug'] == 12.34
    assert vals['trosak_mjesec'] == tariff.izracunaj_racun(10.0, 4.0, 1, vt_udio=0.45)
    assert st['sensor.energija_dug'][0] == 12.34
    assert st['sensor.energija_autarkija_danas'][0] == 75.0

    # inverterski split za 15.7. još ne postoji (puni se jednom dnevno)
    stanje, attrs = st['sensor.energija_inv1_danas']
    assert stanje == 'unavailable'
    assert attrs['unit_of_measurement'] == 'kWh' and 'osvjezavanje' in attrs
    assert st['sensor.energija_inv2_danas'][0] == 'unavailable'


def test_nepoznati_dnevni_podaci_su_unavailable(conn):
    _, st = _push(conn)
    for eid in ('sensor.energija_dug', 'sensor.energija_trosak_mjesec',
                'sensor.energija_trosak_danas', 'sensor.energija_autarkija_danas'):
        assert st[eid][0] == 'unavailable', eid
        assert 'friendly_name' in st[eid][1]
    assert st['sensor.energija_vt_udio'][0] == 45.0
