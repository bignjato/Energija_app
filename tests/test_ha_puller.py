"""Testovi HA pullera (sma_live / sma_dnevna) — HTTP helperi su monkeypatchani."""

import sqlite3
from datetime import datetime, timezone

import pytest

from hepapp import ha_puller

SCHEMA = '''
    CREATE TABLE sma_live (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        pv_generation_w REAL, feed_in_w REAL, external_consumption_w REAL,
        total_consumption_w REAL, direct_consumption_w REAL,
        autarky_rate REAL, self_consumption_rate REAL, battery_soc REAL
    );
    CREATE TABLE sma_dnevna (
        datum TEXT PRIMARY KEY,
        pv_generation_kwh REAL, feed_in_kwh REAL, grid_consumption_kwh REAL,
        total_consumption_kwh REAL, self_consumption_kwh REAL,
        autarky_rate REAL,
        pv_kwh_inv1 REAL, pv_kwh_inv2 REAL
    );
'''

HA_ENV = [
    'HA_ENT_PV_W', 'HA_ENT_FEED_W', 'HA_ENT_CONS_W', 'HA_ENT_GRID_W',
    'HA_ENT_AUTARKY', 'HA_ENT_BATTERY_SOC',
    'HA_ENT_PV_KWH_DAY', 'HA_ENT_FEED_KWH_DAY', 'HA_ENT_CONS_KWH_DAY', 'HA_ENT_GRID_KWH_DAY',
    'HA_ENT_PV_KWH_LIFETIME', 'HA_ENT_FEED_KWH_LIFETIME',
    'HA_ENT_GRID_KWH_LIFETIME', 'HA_ENT_CONS_KWH_LIFETIME',
]


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'ha.db')
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    monkeypatch.setattr(ha_puller, 'DB_PATH', path)
    for k in HA_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('HA_URL', 'http://ha.test')
    monkeypatch.setenv('HA_TOKEN', 'test-token')
    # podne po ljetnom vremenu, ako test ne zamrzne nešto drugo
    _freeze(monkeypatch, datetime(2026, 7, 10, 10, 0, tzinfo=timezone.utc))
    return path


def _freeze(monkeypatch, dt):
    monkeypatch.setattr(ha_puller, '_now_utc', lambda: dt)


def _states(monkeypatch, states):
    """states: {entity_id: (value, unit)}; nepoznat entity -> (None, None)."""
    monkeypatch.setattr(ha_puller, '_get_state',
                        lambda ha_url, token, eid: states.get(eid, (None, None)))


def _day_env(monkeypatch):
    monkeypatch.setenv('HA_ENT_PV_KWH_DAY', 'sensor.pv_day')
    monkeypatch.setenv('HA_ENT_FEED_KWH_DAY', 'sensor.feed_day')
    monkeypatch.setenv('HA_ENT_CONS_KWH_DAY', 'sensor.cons_day')
    monkeypatch.setenv('HA_ENT_GRID_KWH_DAY', 'sensor.grid_day')


def _dnevna(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute('SELECT * FROM sma_dnevna ORDER BY datum')]
    conn.close()
    return rows


# ---------------------------------------------------------------------------
# pull_today
# ---------------------------------------------------------------------------

def test_pull_today_kolona_po_kolona(db, monkeypatch):
    _day_env(monkeypatch)
    # svaka veličina ima različitu vrijednost — zamjena kolona mora pasti
    _states(monkeypatch, {
        'sensor.pv_day':   (11.0, 'kWh'),
        'sensor.feed_day': (3.0, 'kWh'),
        'sensor.cons_day': (20.0, 'kWh'),
        'sensor.grid_day': (5000.0, 'Wh'),   # normalizira se na 5 kWh
    })
    assert ha_puller.pull_today() is True
    rows = _dnevna(db)
    assert len(rows) == 1
    r = rows[0]
    assert r['datum'] == '2026-07-10'
    assert r['pv_generation_kwh'] == 11.0
    assert r['feed_in_kwh'] == 3.0
    assert r['grid_consumption_kwh'] == 5.0
    assert r['total_consumption_kwh'] == 20.0
    assert r['autarky_rate'] == pytest.approx(0.75)   # 1 - 5/20
    # kolone koje puller ne dira
    assert r['self_consumption_kwh'] is None
    assert r['pv_kwh_inv1'] is None and r['pv_kwh_inv2'] is None


def test_pull_today_nize_ocitanje_ne_gazi(db, monkeypatch):
    _day_env(monkeypatch)
    _states(monkeypatch, {
        'sensor.pv_day': (11.0, 'kWh'), 'sensor.feed_day': (3.0, 'kWh'),
        'sensor.cons_day': (20.0, 'kWh'), 'sensor.grid_day': (5.0, 'kWh'),
    })
    assert ha_puller.pull_today()
    # reset senzora / krivi dan: sve blizu nule
    _states(monkeypatch, {
        'sensor.pv_day': (0.1, 'kWh'), 'sensor.feed_day': (0.0, 'kWh'),
        'sensor.cons_day': (0.4, 'kWh'), 'sensor.grid_day': (0.3, 'kWh'),
    })
    assert ha_puller.pull_today()
    r = _dnevna(db)[0]
    assert (r['pv_generation_kwh'], r['feed_in_kwh'],
            r['grid_consumption_kwh'], r['total_consumption_kwh']) == (11.0, 3.0, 5.0, 20.0)
    assert r['autarky_rate'] == pytest.approx(0.75)   # iz zadržanih, ne iz niskih vrijednosti

    # više očitanje normalno prolazi (i miješano: pv raste, grid niži)
    _states(monkeypatch, {
        'sensor.pv_day': (12.5, 'kWh'), 'sensor.feed_day': (3.5, 'kWh'),
        'sensor.cons_day': (24.0, 'kWh'), 'sensor.grid_day': (4.0, 'kWh'),
    })
    assert ha_puller.pull_today()
    r = _dnevna(db)[0]
    assert (r['pv_generation_kwh'], r['feed_in_kwh'],
            r['grid_consumption_kwh'], r['total_consumption_kwh']) == (12.5, 3.5, 5.0, 24.0)
    assert r['autarky_rate'] == pytest.approx(1 - 5.0 / 24.0)


def test_pull_today_none_ne_brise_spremljeno(db, monkeypatch):
    _day_env(monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, pv_kwh_inv1) "
                 "VALUES ('2026-07-10', 9.0, 2.0, 4.5)")
    conn.commit()
    conn.close()
    _states(monkeypatch, {'sensor.grid_day': (1.0, 'kWh')})   # ostali nedostupni
    assert ha_puller.pull_today()
    r = _dnevna(db)[0]
    assert r['pv_generation_kwh'] == 9.0
    assert r['feed_in_kwh'] == 2.0
    assert r['grid_consumption_kwh'] == 1.0
    assert r['total_consumption_kwh'] is None   # balans se ne računa iz starih + novih vrijednosti
    assert r['autarky_rate'] is None
    assert r['pv_kwh_inv1'] == 4.5


def test_pull_today_lokalni_dan_ljeti(db, monkeypatch):
    # 23:30 UTC 14.7. = 01:30 lokalno 15.7. (CEST) -> red za 15.7., ne 14.7.
    _freeze(monkeypatch, datetime(2026, 7, 14, 23, 30, tzinfo=timezone.utc))
    _day_env(monkeypatch)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, "
                 "grid_consumption_kwh, total_consumption_kwh) "
                 "VALUES ('2026-07-14', 31.0, 14.0, 6.0, 23.0)")
    conn.commit()
    conn.close()
    _states(monkeypatch, {
        'sensor.pv_day': (0.0, 'kWh'), 'sensor.feed_day': (0.0, 'kWh'),
        'sensor.cons_day': (0.6, 'kWh'), 'sensor.grid_day': (0.6, 'kWh'),
    })
    assert ha_puller.pull_today()
    rows = {r['datum']: r for r in _dnevna(db)}
    assert set(rows) == {'2026-07-14', '2026-07-15'}
    # jučerašnji red netaknut
    assert rows['2026-07-14']['pv_generation_kwh'] == 31.0
    assert rows['2026-07-14']['total_consumption_kwh'] == 23.0
    assert rows['2026-07-15']['total_consumption_kwh'] == 0.6
    assert rows['2026-07-15']['autarky_rate'] == 0.0


def test_pull_today_lokalni_dan_zimi(db, monkeypatch):
    # 23:30 UTC 14.1. = 00:30 lokalno 15.1. (CET)
    _freeze(monkeypatch, datetime(2026, 1, 14, 23, 30, tzinfo=timezone.utc))
    _day_env(monkeypatch)
    _states(monkeypatch, {'sensor.pv_day': (0.0, 'kWh')})
    assert ha_puller.pull_today()
    assert [r['datum'] for r in _dnevna(db)] == ['2026-01-15']


def test_pull_today_jedan_od_dva_nedostupan(db, monkeypatch, caplog):
    monkeypatch.setenv('HA_ENT_PV_KWH_DAY', 'sensor.pv_a, sensor.pv_b')
    monkeypatch.setenv('HA_ENT_FEED_KWH_DAY', 'sensor.feed_day')
    _states(monkeypatch, {
        'sensor.pv_a': (7.0, 'kWh'),
        'sensor.pv_b': (None, 'kWh'),        # unavailable
        'sensor.feed_day': (2.0, 'kWh'),
    })
    with caplog.at_level('WARNING', logger='ha_puller'):
        assert ha_puller.pull_today()
    r = _dnevna(db)[0]
    assert r['pv_generation_kwh'] is None    # NE 7.0 (djelomična suma)
    assert r['feed_in_kwh'] == 2.0
    assert 'sensor.pv_b' in caplog.text


def test_pull_today_autarkija_granice(db, monkeypatch):
    _day_env(monkeypatch)
    # grid > cons (nekonzistentni senzori) -> 0, ne negativno
    _states(monkeypatch, {'sensor.cons_day': (2.0, 'kWh'), 'sensor.grid_day': (3.0, 'kWh')})
    assert ha_puller.pull_today()
    assert _dnevna(db)[0]['autarky_rate'] == 0.0


def _ubaci_dan(path, datum, pv, feed, grid, cons):
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh, "
                 "grid_consumption_kwh, total_consumption_kwh) VALUES (?,?,?,?,?)",
                 (datum, pv, feed, grid, cons))
    conn.commit()
    conn.close()


def _vrijednosti(r):
    return (r['pv_generation_kwh'], r['feed_in_kwh'],
            r['grid_consumption_kwh'], r['total_consumption_kwh'])


def test_pull_today_citanje_preko_ponoci_ne_upisuje(db, monkeypatch):
    # senzori se čitaju u 23:59:59 lokalno (21:59:59 UTC, CEST), a sat prijeđe
    # ponoć prije upisa: jučerašnji totali ne smiju završiti ni u jednom danu
    _day_env(monkeypatch)
    _ubaci_dan(db, '2026-07-14', 31.0, 14.0, 6.0, 23.0)
    sat = {'now': datetime(2026, 7, 14, 21, 59, 59, tzinfo=timezone.utc)}
    monkeypatch.setattr(ha_puller, '_now_utc', lambda: sat['now'])
    states = {
        'sensor.pv_day': (31.0, 'kWh'), 'sensor.feed_day': (14.0, 'kWh'),
        'sensor.cons_day': (23.4, 'kWh'), 'sensor.grid_day': (6.2, 'kWh'),
    }

    def _spori_get_state(ha_url, token, eid):
        if eid == 'sensor.cons_day':      # usred čitanja prolazi ponoć
            sat['now'] = datetime(2026, 7, 14, 22, 0, 2, tzinfo=timezone.utc)
        return states.get(eid, (None, None))

    monkeypatch.setattr(ha_puller, '_get_state', _spori_get_state)
    assert ha_puller.pull_today() is True
    rows = {r['datum']: r for r in _dnevna(db)}
    assert set(rows) == {'2026-07-14'}                       # nema reda za 15.7.
    assert _vrijednosti(rows['2026-07-14']) == (31.0, 14.0, 6.0, 23.0)

    # sljedeći pull (00:05 lokalno, senzori resetirani) normalno otvara novi dan
    sat['now'] = datetime(2026, 7, 14, 22, 5, tzinfo=timezone.utc)
    _states(monkeypatch, {
        'sensor.pv_day': (0.0, 'kWh'), 'sensor.feed_day': (0.0, 'kWh'),
        'sensor.cons_day': (0.1, 'kWh'), 'sensor.grid_day': (0.1, 'kWh'),
    })
    assert ha_puller.pull_today()
    rows = {r['datum']: r for r in _dnevna(db)}
    assert _vrijednosti(rows['2026-07-15']) == (0.0, 0.0, 0.1, 0.1)


def test_pull_today_senzor_se_jos_nije_resetirao(db, monkeypatch):
    # 00:02 lokalno 17.11. (CET = 23:02 UTC 16.11.): HA dnevni senzori još drže
    # jučerašnje finalne vrijednosti (malo više od zadnjeg upisa u 23:57)
    _freeze(monkeypatch, datetime(2026, 11, 16, 23, 2, tzinfo=timezone.utc))
    _day_env(monkeypatch)
    _ubaci_dan(db, '2026-11-16', 31.0, 14.0, 9.0, 22.0)
    _states(monkeypatch, {
        'sensor.pv_day': (31.0, 'kWh'), 'sensor.feed_day': (14.0, 'kWh'),
        'sensor.cons_day': (22.2, 'kWh'), 'sensor.grid_day': (9.1, 'kWh'),
    })
    assert ha_puller.pull_today() is True
    assert [r['datum'] for r in _dnevna(db)] == ['2026-11-16']   # novi dan NIJE zasijan

    # 00:07: senzori resetirani -> normalan prvi upis
    _freeze(monkeypatch, datetime(2026, 11, 16, 23, 7, tzinfo=timezone.utc))
    _states(monkeypatch, {
        'sensor.pv_day': (0.0, 'kWh'), 'sensor.feed_day': (0.0, 'kWh'),
        'sensor.cons_day': (0.1, 'kWh'), 'sensor.grid_day': (0.1, 'kWh'),
    })
    assert ha_puller.pull_today()
    # 18:00 oblačnog dana: stvarne (od jučer niže) vrijednosti prolaze
    _freeze(monkeypatch, datetime(2026, 11, 17, 17, 0, tzinfo=timezone.utc))
    _states(monkeypatch, {
        'sensor.pv_day': (4.0, 'kWh'), 'sensor.feed_day': (1.0, 'kWh'),
        'sensor.cons_day': (11.0, 'kWh'), 'sensor.grid_day': (8.0, 'kWh'),
    })
    assert ha_puller.pull_today()
    rows = {r['datum']: r for r in _dnevna(db)}
    assert _vrijednosti(rows['2026-11-17']) == (4.0, 1.0, 8.0, 11.0)
    assert rows['2026-11-17']['autarky_rate'] == pytest.approx(1 - 8.0 / 11.0)
    assert _vrijednosti(rows['2026-11-16']) == (31.0, 14.0, 9.0, 22.0)


def test_pull_today_pv_drzi_jucerasnje_do_jutra(db, monkeypatch):
    # PV senzor drži jučerašnji total dok se inverter ne probudi; feed/grid su
    # resetirani; potrošnja se računa iz balansa (nema CONS senzora)
    _freeze(monkeypatch, datetime(2026, 11, 17, 2, 0, tzinfo=timezone.utc))
    monkeypatch.setenv('HA_ENT_PV_KWH_DAY', 'sensor.pv_day')
    monkeypatch.setenv('HA_ENT_FEED_KWH_DAY', 'sensor.feed_day')
    monkeypatch.setenv('HA_ENT_GRID_KWH_DAY', 'sensor.grid_day')
    _ubaci_dan(db, '2026-11-16', 25.0, 10.0, 9.0, 24.0)
    _states(monkeypatch, {
        'sensor.pv_day': (25.0, 'kWh'), 'sensor.feed_day': (0.0, 'kWh'),
        'sensor.grid_day': (0.4, 'kWh'),
    })
    assert ha_puller.pull_today()
    r = {x['datum']: x for x in _dnevna(db)}['2026-11-17']
    # ni PV ni iz njega izveden balans (25 - 0 + 0.4) ne smiju u red
    assert _vrijednosti(r) == (None, 0.0, 0.4, None)
    assert r['autarky_rate'] is None

    # jutro: inverter budan, PV kreće od nule
    _freeze(monkeypatch, datetime(2026, 11, 17, 8, 0, tzinfo=timezone.utc))
    _states(monkeypatch, {
        'sensor.pv_day': (0.5, 'kWh'), 'sensor.feed_day': (0.1, 'kWh'),
        'sensor.grid_day': (1.6, 'kWh'),
    })
    assert ha_puller.pull_today()
    r = {x['datum']: x for x in _dnevna(db)}['2026-11-17']
    assert _vrijednosti(r) == (0.5, 0.1, 1.6, pytest.approx(2.0))


def test_pull_today_danas_smije_preci_jucerasnji_total(db, monkeypatch):
    # zaštita "nije resetirano" vrijedi samo za PRVI upis u danu; kad današnji
    # red već postoji, vrijednost normalno raste i preko jučerašnjeg totala.
    # Jučerašnja nula (npr. bez predaje) nije dokaz ničega.
    _day_env(monkeypatch)
    _ubaci_dan(db, '2026-07-09', 5.0, 0.0, 6.0, 10.0)
    _ubaci_dan(db, '2026-07-10', 4.0, None, 5.0, 9.0)
    _states(monkeypatch, {
        'sensor.pv_day': (6.0, 'kWh'), 'sensor.feed_day': (0.0, 'kWh'),
        'sensor.cons_day': (12.0, 'kWh'), 'sensor.grid_day': (7.0, 'kWh'),
    })
    assert ha_puller.pull_today()
    r = {x['datum']: x for x in _dnevna(db)}['2026-07-10']
    assert _vrijednosti(r) == (6.0, 0.0, 7.0, 12.0)


# ---------------------------------------------------------------------------
# _sum_entities / _today_delta / _midnight_state
# ---------------------------------------------------------------------------

def test_sum_entities(monkeypatch, caplog):
    _states(monkeypatch, {
        'sensor.a': (1.5, 'kW'), 'sensor.b': (700.0, 'W'), 'sensor.c': (None, 'W'),
    })
    assert ha_puller._sum_entities('u', 't', 'sensor.a,sensor.b') == 2200.0
    with caplog.at_level('WARNING', logger='ha_puller'):
        assert ha_puller._sum_entities('u', 't', 'sensor.a,sensor.c') is None
    assert 'sensor.c' in caplog.text
    assert ha_puller._sum_entities('u', 't', 'sensor.c') is None
    assert ha_puller._sum_entities('u', 't', '') is None
    assert ha_puller._sum_entities('u', 't', None) is None


def test_today_delta_bez_djelomicne_sume(monkeypatch):
    _states(monkeypatch, {'sensor.l1': (1010.0, 'kWh'), 'sensor.l2': (2020.0, 'kWh')})
    ponoc = {'sensor.l1': (1000.0, 'kWh'), 'sensor.l2': (None, None)}
    monkeypatch.setattr(ha_puller, '_midnight_state', lambda u, t, eid: ponoc[eid])
    assert ha_puller._today_delta('u', 't', 'sensor.l1,sensor.l2') is None
    ponoc['sensor.l2'] = (2005.0, 'kWh')
    assert ha_puller._today_delta('u', 't', 'sensor.l1,sensor.l2') == pytest.approx(25.0)


class _Resp:
    status_code = 200

    def json(self):
        return [[{'state': '123.4', 'attributes': {'unit_of_measurement': 'kWh'}}]]


class _Session:
    def __init__(self):
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        return _Resp()


@pytest.mark.parametrize('now_utc, ocekivano', [
    # ljeto: lokalna ponoć 15.7. = 22:00 UTC 14.7.
    (datetime(2026, 7, 14, 23, 30, tzinfo=timezone.utc), '2026-07-14T22:00:00+00:00'),
    (datetime(2026, 7, 15, 10, 0, tzinfo=timezone.utc),  '2026-07-14T22:00:00+00:00'),
    # zima: lokalna ponoć 15.1. = 23:00 UTC 14.1.
    (datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc),  '2026-01-14T23:00:00+00:00'),
])
def test_midnight_state_lokalna_ponoc(monkeypatch, now_utc, ocekivano):
    _freeze(monkeypatch, now_utc)
    sess = _Session()
    monkeypatch.setattr(ha_puller, 'SESSION', sess)
    assert ha_puller._midnight_state('http://ha.test', 'tok', 'sensor.l1') == (123.4, 'kWh')
    assert sess.urls == [f'http://ha.test/api/history/period/{ocekivano}']


# ---------------------------------------------------------------------------
# pull_recent
# ---------------------------------------------------------------------------

def _live(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute('SELECT * FROM sma_live ORDER BY id')]
    conn.close()
    return rows


def _recent_env(monkeypatch):
    monkeypatch.setenv('HA_ENT_PV_W', 'sensor.pv1,sensor.pv2')
    monkeypatch.setenv('HA_ENT_FEED_W', 'sensor.feed')
    monkeypatch.setenv('HA_ENT_GRID_W', 'sensor.grid')
    monkeypatch.setenv('HA_ENT_CONS_W', 'sensor.cons')
    monkeypatch.setenv('HA_ENT_AUTARKY', 'sensor.aut')
    monkeypatch.setenv('HA_ENT_BATTERY_SOC', 'sensor.soc')


def test_pull_recent_kolone_i_format_ts(db, monkeypatch):
    _freeze(monkeypatch, datetime(2026, 11, 15, 22, 48, 35, 123456, tzinfo=timezone.utc))
    _recent_env(monkeypatch)
    _states(monkeypatch, {
        'sensor.pv1': (1.2, 'kW'), 'sensor.pv2': (800.0, 'W'),
        'sensor.feed': (350.0, 'W'), 'sensor.grid': (40.0, 'W'),
        'sensor.cons': (1690.0, 'W'), 'sensor.aut': (97.0, '%'), 'sensor.soc': (55.0, '%'),
    })
    assert ha_puller.pull_recent() is True
    r = _live(db)[0]
    assert r['ts'] == '2026-11-15T22:48:35+00:00'
    assert r['pv_generation_w'] == 2000.0
    assert r['feed_in_w'] == 350.0
    assert r['external_consumption_w'] == 40.0
    assert r['total_consumption_w'] == 1690.0
    assert r['autarky_rate'] == pytest.approx(0.97)
    assert r['battery_soc'] == 55.0


@pytest.mark.parametrize('state, ocekivano', [
    ((1.0, '%'), 0.01),     # 1 % nije 100 %
    ((100.0, '%'), 1.0),
    ((0.5, '%'), 0.005),
    ((0.8, ''), 0.8),       # bez unita: stara heuristika
    ((80.0, ''), 0.8),
    ((80.0, None), 0.8),
])
def test_pull_recent_autarkija_po_unitu(db, monkeypatch, state, ocekivano):
    _recent_env(monkeypatch)
    _states(monkeypatch, {
        'sensor.pv1': (1000.0, 'W'), 'sensor.pv2': (1000.0, 'W'),
        'sensor.feed': (0.0, 'W'), 'sensor.grid': (0.0, 'W'), 'sensor.cons': (2000.0, 'W'),
        'sensor.aut': state,
    })
    assert ha_puller.pull_recent()
    assert _live(db)[0]['autarky_rate'] == pytest.approx(ocekivano)


def test_pull_recent_djelomicna_lista_je_none(db, monkeypatch):
    _recent_env(monkeypatch)
    monkeypatch.delenv('HA_ENT_CONS_W')
    _states(monkeypatch, {
        'sensor.pv1': (1000.0, 'W'), 'sensor.pv2': (None, 'W'),
        'sensor.feed': (100.0, 'W'), 'sensor.grid': (50.0, 'W'),
    })
    assert ha_puller.pull_recent()
    r = _live(db)[0]
    assert r['pv_generation_w'] is None        # ne 1000 (samo jedan inverter)
    assert r['feed_in_w'] == 100.0
    assert r['total_consumption_w'] is None    # balans se ne računa bez PV-a
