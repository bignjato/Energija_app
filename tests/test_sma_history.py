"""Testovi SMA history importa (dnevna agregacija + ponašanje kod neuspjelog tjedna)."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import sma_history_import as shi


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(tmp_path / 'sma.db')
    c.executescript('''
        CREATE TABLE sma_15min (
            ts TEXT PRIMARY KEY,
            pv_generation_wh REAL, feed_in_wh REAL, grid_consumption_wh REAL,
            total_consumption_wh REAL,
            pv_w_inv1 REAL DEFAULT 0, pv_w_inv2 REAL DEFAULT 0, pv_w_total REAL DEFAULT 0
        );
        CREATE TABLE sma_dnevna (
            datum TEXT PRIMARY KEY,
            pv_generation_kwh REAL, feed_in_kwh REAL, grid_consumption_kwh REAL,
            total_consumption_kwh REAL, self_consumption_kwh REAL,
            autarky_rate REAL,
            pv_kwh_inv1 REAL, pv_kwh_inv2 REAL
        );
    ''')
    yield c
    c.close()


def _sample(conn, ts, w1, w2):
    conn.execute('INSERT INTO sma_15min (ts,pv_w_inv1,pv_w_inv2,pv_w_total) VALUES (?,?,?,?)',
                 (ts, w1, w2, w1 + w2))


def _dan(conn, datum):
    conn.row_factory = sqlite3.Row
    return conn.execute('SELECT * FROM sma_dnevna WHERE datum=?', (datum,)).fetchone()


def test_aggregate_cuva_kolone_drugih_jobova(conn):
    conn.execute('''INSERT INTO sma_dnevna (datum, pv_generation_kwh, feed_in_kwh,
                        grid_consumption_kwh, total_consumption_kwh, autarky_rate)
                    VALUES ('2026-07-10', 1.0, 11.1, 2.2, 13.3, 0.77)''')
    _sample(conn, '2026-07-10T10:00:00Z', 4000, 2000)
    _sample(conn, '2026-07-10T10:15:00Z', 2000, 0)
    conn.commit()

    shi.aggregate_daily(conn)
    shi.aggregate_daily(conn)  # idempotentno

    r = _dan(conn, '2026-07-10')
    assert r['pv_generation_kwh'] == pytest.approx(2.0)   # 8000 W * 0.25 h
    assert r['pv_kwh_inv1'] == pytest.approx(1.5)
    assert r['pv_kwh_inv2'] == pytest.approx(0.5)
    assert r['feed_in_kwh'] == 11.1
    assert r['grid_consumption_kwh'] == 2.2
    assert r['total_consumption_kwh'] == 13.3
    assert r['autarky_rate'] == 0.77


def test_aggregate_lokalni_dan(conn):
    # 22:30Z ljeti = 00:30 CEST idućeg dana; zimi 22:30Z = 23:30 CET istog dana
    _sample(conn, '2026-07-10T22:30:00Z', 400, 0)
    _sample(conn, '2026-01-10T22:30:00Z', 800, 0)
    _sample(conn, '2026-01-10T23:00:00Z', 1200, 0)
    conn.commit()

    shi.aggregate_daily(conn)

    assert _dan(conn, '2026-07-10') is None
    assert _dan(conn, '2026-07-11')['pv_generation_kwh'] == pytest.approx(0.1)
    assert _dan(conn, '2026-01-10')['pv_generation_kwh'] == pytest.approx(0.2)
    assert _dan(conn, '2026-01-11')['pv_generation_kwh'] == pytest.approx(0.3)


def test_aggregate_dan_bez_proizvodnje_dobije_red(conn):
    _sample(conn, '2025-12-01T10:00:00Z', 0, 0)
    _sample(conn, '2025-12-01T10:15:00Z', 0, 0)
    conn.commit()

    shi.aggregate_daily(conn)

    r = _dan(conn, '2025-12-01')
    assert r is not None
    assert r['pv_generation_kwh'] == 0
    assert r['feed_in_kwh'] is None


def test_aggregate_nula_ne_gazi_postojecu_pv_vrijednost(conn):
    conn.execute("INSERT INTO sma_dnevna (datum, pv_generation_kwh) VALUES ('2025-12-02', 5.5)")
    _sample(conn, '2025-12-02T10:00:00Z', 0, 0)
    conn.commit()

    shi.aggregate_daily(conn)

    assert _dan(conn, '2025-12-02')['pv_generation_kwh'] == 5.5


def test_aggregate_tekuci_dan_ne_dira_pv_total(conn):
    # Danas: ukupni PV pripada ha_pulleru (prima samo rastuće vrijednosti) —
    # precijenjeni djelomični zbroj ga ne smije ni upisati ni pregaziti.
    sad = datetime.now(timezone.utc)
    danas = datetime.now().strftime('%Y-%m-%d')
    jucer_utc = sad - timedelta(days=2)
    _sample(conn, _ts(sad), 4000, 2000)
    _sample(conn, _ts(jucer_utc), 4000, 0)
    conn.commit()

    # red za danas još ne postoji -> nastaje bez ukupnog PV-a, ali s inv1/inv2
    shi.aggregate_daily(conn)
    r = _dan(conn, danas)
    assert r['pv_generation_kwh'] is None
    assert r['pv_kwh_inv1'] == pytest.approx(1.0)
    assert r['pv_kwh_inv2'] == pytest.approx(0.5)
    # završeni dan i dalje dobije ukupni PV
    stari = conn.execute('SELECT pv_generation_kwh FROM sma_dnevna WHERE datum<>?', (danas,)).fetchall()
    assert [x[0] for x in stari] == [pytest.approx(1.0)]

    # HA je u međuvremenu upisao 1.2 kWh (manje od Max-zbroja 1.5+0.75) -> ostaje 1.2
    conn.execute('UPDATE sma_dnevna SET pv_generation_kwh=1.2, feed_in_kwh=0.4 WHERE datum=?', (danas,))
    _sample(conn, _ts(sad + timedelta(seconds=1)), 2000, 1000)
    conn.commit()
    shi.aggregate_daily(conn)
    r = _dan(conn, danas)
    assert r['pv_generation_kwh'] == 1.2
    assert r['feed_in_kwh'] == 0.4
    assert r['pv_kwh_inv1'] == pytest.approx(1.5)
    assert r['pv_kwh_inv2'] == pytest.approx(0.75)


# ---- main(): neuspjeli tjedan ----

def _ts(dt):
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ')


def _main_setup(conn, monkeypatch, tmp_path, fetch):
    zadnji = (datetime.now(timezone.utc) - timedelta(days=20)).replace(
        minute=0, second=0, microsecond=0)
    _sample(conn, _ts(zadnji), 100, 0)
    conn.commit()
    monkeypatch.setattr(shi, 'DB_PATH', str(tmp_path / 'sma.db'))
    monkeypatch.setattr(shi, 'get_token', lambda: 'tok')
    monkeypatch.setattr(shi, 'fetch_week', fetch)
    return zadnji


def test_main_neuspjeli_tjedan_ne_napreduje(conn, monkeypatch, tmp_path):
    pozivi = []

    def fetch(token, b, e, strict=False):
        pozivi.append((b, strict))
        if len(pozivi) == 2:
            raise RuntimeError('SMA API 500')
        t = datetime.strptime(b, '%Y-%m-%dT%H:%M:%S.000Z')
        return {_ts(t): 500.0}, {}

    zadnji = _main_setup(conn, monkeypatch, tmp_path, fetch)

    assert shi.main() == 1
    # 20 dana = 3 tjedna; drugi pada -> treći se ne smije ni pokušati
    assert len(pozivi) == 2
    assert all(strict for _, strict in pozivi)
    prvi_tjedan = zadnji + timedelta(minutes=15)
    assert conn.execute('SELECT MAX(ts) FROM sma_15min').fetchone()[0] == _ts(prvi_tjedan)

    # sljedeći run kreće od iste točke (odmah iza zadnjeg uspješno spremljenog uzorka)
    pozivi.clear()
    monkeypatch.setattr(shi, 'fetch_week',
                        lambda token, b, e, strict=False: (pozivi.append(b) or ({}, {})))
    assert shi.main() == 0
    assert pozivi[0] == (prvi_tjedan + timedelta(minutes=15)).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def test_main_greska_usred_tjedna_radi_rollback(conn, monkeypatch, tmp_path):
    # Greška tek NAKON prvog uspješnog INSERT-a: bez rollbacka bi taj red
    # commitao aggregate_daily i MAX(ts) bi preskočio nespremljene uzorke.
    class PucaOdDrugog(dict):
        n = 0

        def get(self, k, d=None):
            PucaOdDrugog.n += 1
            if PucaOdDrugog.n >= 2:
                raise RuntimeError('pokvaren odgovor')
            return d

    def fetch(token, b, e, strict=False):
        t = datetime.strptime(b, '%Y-%m-%dT%H:%M:%S.000Z')
        inv1 = {_ts(t + timedelta(minutes=15 * i)): 500.0 for i in range(3)}
        return inv1, PucaOdDrugog()

    zadnji = _main_setup(conn, monkeypatch, tmp_path, fetch)

    assert shi.main() == 1
    assert PucaOdDrugog.n == 2          # jedan INSERT je prošao prije greške
    assert conn.execute('SELECT MAX(ts), COUNT(*) FROM sma_15min').fetchone() == (_ts(zadnji), 1)


def test_fetch_week_strict_baca_na_http_gresku(monkeypatch):
    class R:
        status_code = 503

    monkeypatch.setattr(shi.requests, 'post', lambda *a, **k: R())
    # default (maintenance.backfill_range) i dalje vraća prazno
    assert shi.fetch_week('tok', 'b', 'e') == ({}, {})
    with pytest.raises(RuntimeError):
        shi.fetch_week('tok', 'b', 'e', strict=True)
